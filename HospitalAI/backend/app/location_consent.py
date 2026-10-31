"""Atomic updates of location consent and its revision."""

from contextlib import closing
from typing import Literal

from app.db import Database
from app.location_reset import enqueue_location_revocation
from app.recommendation_turns import invalidate_location_contexts
from app.utterance_location import validate_source_generation

def set_location_consent_with_epoch(database: Database, call_id: str, accepted: bool, *,
                                   source_location_generation: int | None = None,
                                   source_location_revision: int | None = None) -> tuple[Literal["accepted", "revoked"], int, int, int] | None:
    """Record separate location consent and cancel queued unsent deliveries on withdrawal."""
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        call = con.execute("SELECT state,consent_version,ended_at FROM calls WHERE id=?", (call_id,)).fetchone()
        if call is None or call["state"] != "accepted" or (accepted and call["ended_at"]):
            return None
        if accepted and not validate_source_generation(
                con, call_id, source_location_generation, source_location_revision):
            return None
        row = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
        state: Literal["accepted", "revoked"] = "accepted" if accepted else "revoked"
        if row is not None and row["state"] == state:
            revision = int(con.execute("SELECT COALESCE(MAX(id),0) FROM location_consent_events WHERE call_id=?", (call_id,)).fetchone()[0])
            return state, 0, int(row["generation"]) if accepted else 0, revision
        generation = int(row["generation"]) if row is not None else 0
        if accepted:
            generation += 1
        con.execute("""INSERT INTO call_location_consent(call_id,state,generation) VALUES (?,?,?)
            ON CONFLICT(call_id) DO UPDATE SET state=excluded.state,generation=excluded.generation,
            changed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""", (call_id, state, generation))
        revision = int(con.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,?,?)",
                                   (call_id, state, generation)).lastrowid)
        cancelled = 0
        if not accepted:
            invalidate_location_contexts(con, call_id)
            cursor = con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
                SELECT event_id FROM recommendation_outbox o WHERE call_id=? AND state='pending'
                AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)""", (call_id,))
            cancelled = cursor.rowcount
            if generation > 0 and call["consent_version"] in ("municipality-v2", "municipality-v3"):
                enqueue_location_revocation(con, call_id, generation)
        return state, cancelled, generation if accepted else 0, revision

