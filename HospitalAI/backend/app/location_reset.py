"""Atomic location-subject resets and withdrawal notifications."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.db import Database, DuplicateRequest
from app.call_facts import mark_patient_subject_change
from app.recommendation_turns import invalidate_location_contexts
from app.utterance_location import validate_source_generation

def enqueue_location_revocation(con: sqlite3.Connection, call_id: str, generation: int) -> None:
    """Persist a control event that remains deliverable after consent withdrawal."""
    session = con.execute("SELECT phone FROM sessions WHERE id=?", (call_id,)).fetchone()
    event_id = f"location-revoked:{call_id}:{generation}"
    body = json.dumps({"source": "hospital-ai", "event_id": event_id, "call_id": call_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(), "event_type": "location_revoked",
        "phone": session["phone"], "user_text": "", "assistant_text": "", "answer_status": None,
        "sources": [], "location_generation": generation}, ensure_ascii=False, separators=(",", ":"))
    con.execute("""INSERT OR IGNORE INTO municipal_location_revocations
        (event_id,call_id,generation,body) VALUES (?,?,?,?)""", (event_id, call_id, generation, body))


def reset_location_generation(database: Database, call_id: str, request_id: str,
                              source_location_generation: int | None = None,
                              source_location_revision: int | None = None) -> int | None:
    result = reset_location_generation_with_epoch(database, call_id, request_id,
                                                   source_location_generation, source_location_revision)
    return result[0] if result is not None else None


def reset_location_generation_with_epoch(database: Database, call_id: str, request_id: str,
                                         source_location_generation: int | None = None,
                                         source_location_revision: int | None = None) -> tuple[int, int] | None:
    """Forget caller-device location after a patient-subject change, retaining consent."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
        raise DuplicateRequest("Invalid location reset request ID")
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        call = con.execute("""SELECT c.state,c.urgent,c.ended_at,c.consent_version,l.state AS location_state,
            l.generation FROM calls c JOIN call_location_consent l ON l.call_id=c.id WHERE c.id=?""",
            (call_id,)).fetchone()
        if call is None or call["state"] != "accepted" or call["urgent"] or call["ended_at"] or call["location_state"] != "accepted":
            return None
        prior = con.execute("SELECT generation FROM call_location_resets WHERE call_id=? AND request_id=?",
                            (call_id, request_id)).fetchone()
        if prior is not None:
            prior_generation = int(prior["generation"])
            if int(call["generation"]) != prior_generation:
                return None
            if source_location_generation not in (None, prior_generation - 1, prior_generation):
                return None
            if source_location_revision is not None:
                previous_revision = con.execute("""SELECT MAX(id) FROM location_consent_events
                    WHERE call_id=? AND generation=? AND state='accepted'""", (call_id, prior_generation - 1)).fetchone()[0]
                if previous_revision != source_location_revision:
                    return None
            revision = con.execute("""SELECT MAX(id) FROM location_consent_events
                WHERE call_id=? AND generation=? AND state='accepted'""", (call_id, prior_generation)).fetchone()[0]
            return (prior_generation, int(revision)) if revision is not None else None
        if not validate_source_generation(con, call_id, source_location_generation, source_location_revision):
            return None
        tables = {row[0] for row in con.execute("""SELECT name FROM sqlite_master WHERE type='table'
            AND name IN ('call_fact_subject_boundaries','pending_utterances')""")}
        if len(tables) != 2 or not mark_patient_subject_change(con, call_id, request_id):
            return None
        previous = int(call["generation"])
        generation = previous + 1
        con.execute("""UPDATE call_location_consent SET generation=?,
            changed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE call_id=?""", (generation, call_id))
        revision = int(con.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,'accepted',?)",
                                   (call_id, generation)).lastrowid)
        invalidate_location_contexts(con, call_id)
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_recommendation_navigation'").fetchone():
            con.execute("UPDATE call_recommendation_navigation SET chosen_specialty=NULL WHERE call_id=?", (call_id,))
        con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
            SELECT event_id FROM recommendation_outbox WHERE call_id=? AND state='pending'""", (call_id,))
        if previous > 0 and call["consent_version"] in ("municipality-v2", "municipality-v3"):
            enqueue_location_revocation(con, call_id, previous)
        con.execute("INSERT INTO call_location_resets(call_id,request_id,generation) VALUES (?,?,?)",
                    (call_id, request_id, generation))
        return generation, revision

