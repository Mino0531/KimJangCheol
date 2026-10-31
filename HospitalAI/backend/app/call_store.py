"""Consent state for calls in the local SQLite prototype."""

import sqlite3
from contextlib import closing
from typing import Literal
from uuid import uuid4

from app.audio_store import Audio
from app.db import WRITABLE, Database, DuplicateRequest, InvalidTurnFields, Row
from app.recommendation_turns import context_turn_current, invalidate_context_turn
from app.schemas import TurnInput
from app.services import (
    EMERGENCY_MESSAGE,
    PESTICIDE_EMERGENCY_MESSAGE,
    emergency_detected,
    pesticide_risk,
)
from app.utterance_location import (
    StaleLocationGeneration,
    capture_location_generation,
    capture_location_revision,
    urgent_detail_hash,
    user_generation_visible,
    validate_source_generation,
)
from app.fall_guidance import fall_urgent, has_fall, last_guidance, mentions_other_person, same_relative_with_fall, visible_guidance, walking_answer

CallState = Literal["awaiting_consent", "accepted", "declined"]


def _mark_urgent(con: sqlite3.Connection, session_id: str) -> bool:
    cursor = con.execute("UPDATE calls SET urgent = 1 WHERE id = ? AND ended_at IS NULL", (session_id,))
    if cursor.rowcount == 0:
        return False
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_cancellations'").fetchone():
        con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
            SELECT event_id FROM recommendation_outbox WHERE call_id = ? AND state = 'pending'""", (session_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generated_audio'").fetchone():
        con.execute("""DELETE FROM generated_audio WHERE turn_id IN
            (SELECT id FROM turns WHERE session_id = ? AND status IN ('pending', 'processing'))""", (session_id,))
    con.execute("""UPDATE turns SET status = 'cancelled', initial_text = NULL, final_text = NULL,
        sources_json = NULL, tts_json = NULL, guidance_json = NULL, guidance_delivered = 0, error = 'urgent_override',
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE session_id = ? AND status IN ('pending', 'processing')""", (session_id,))
    return True


class CallStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY REFERENCES sessions(id),
                state TEXT NOT NULL DEFAULT 'awaiting_consent',
                urgent INTEGER NOT NULL DEFAULT 0,
                consent_version TEXT NOT NULL DEFAULT 'prototype-v1',
                decision_at TEXT,
                ended_at TEXT,
                end_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(calls)")}
            if "urgent" not in columns:
                con.execute("ALTER TABLE calls ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
            if "ended_at" not in columns:
                con.execute("ALTER TABLE calls ADD COLUMN ended_at TEXT")
            if "end_reason" not in columns:
                con.execute("ALTER TABLE calls ADD COLUMN end_reason TEXT")
            con.execute("""CREATE TABLE IF NOT EXISTS urgent_details (
                call_id TEXT PRIMARY KEY REFERENCES calls(id),
                text TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS urgent_detail_events (
                call_id TEXT NOT NULL REFERENCES calls(id),
                text_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (call_id,text_hash)
            )""")
            for table in ('urgent_details', 'urgent_detail_events'):
                detail_columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
                if 'user_location_generation' not in detail_columns:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN user_location_generation INTEGER')
                if 'user_location_revision' not in detail_columns:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN user_location_revision INTEGER')
            previous = con.execute("SELECT call_id,text,recorded_at,user_location_generation,user_location_revision FROM urgent_details").fetchall()
            for row in previous:
                text = str(row["text"])
                digest = urgent_detail_hash(text, row['user_location_generation'], row['user_location_revision'])
                con.execute("""INSERT OR IGNORE INTO urgent_detail_events
                    (call_id,text_hash,text,recorded_at,user_location_generation,user_location_revision) VALUES (?,?,?,?,?,?)""",
                    (row["call_id"], digest, text, row["recorded_at"], row['user_location_generation'], row['user_location_revision']))
        # New calls record location consent together with the call decision.
        from app.recommend_dashboard import RecommendationOutbox
        RecommendationOutbox(self.database).initialize()

    def create(self, session_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("INSERT INTO calls(id, consent_version) VALUES (?, ?)", (session_id, "municipality-v3"))

    def get(self, session_id: str) -> Row | None:
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN')
            row = con.execute("SELECT * FROM calls WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return {**dict(row), 'location_generation': capture_location_generation(con, session_id),
                    'location_revision': capture_location_revision(con, session_id)}

    def list(self) -> list[Row]:
        with closing(self.database.connect()) as con:
            rows = con.execute("""SELECT calls.id, calls.state, calls.created_at, sessions.phone
                FROM calls JOIN sessions ON sessions.id = calls.id
                ORDER BY calls.created_at DESC, calls.rowid DESC LIMIT 100""")
            return [dict(row) for row in rows]

    def decide(self, session_id: str, state: CallState) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            result = con.execute("""UPDATE calls SET state = ?,
                decision_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id = ? AND state = 'awaiting_consent' AND urgent = 0 AND ended_at IS NULL""", (state, session_id))
            if result.rowcount != 1:
                return False
            call = con.execute("SELECT consent_version FROM calls WHERE id = ?", (session_id,)).fetchone()
            if state == "accepted" and call["consent_version"] == "municipality-v3":
                con.execute("""INSERT INTO call_location_consent(call_id,state,generation)
                    VALUES (?, 'accepted', 1)""", (session_id,))
                con.execute("""INSERT INTO location_consent_events(call_id,state,generation)
                    VALUES (?, 'accepted', 1)""", (session_id,))
            return True

    def has_call(self, session_id: str) -> bool:
        with closing(self.database.connect()) as con:
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
            return bool(exists and con.execute("SELECT 1 FROM calls WHERE id = ?", (session_id,)).fetchone())

    def is_urgent(self, session_id: str) -> bool:
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT urgent FROM calls WHERE id = ?", (session_id,)).fetchone()
            return bool(row and row["urgent"])

    def mark_urgent(self, session_id: str, text: str | None = None,
                    source_location_generation: int | None = None,
                    source_location_revision: int | None = None) -> bool:
        """Set urgent state and atomically suppress unfinished routine outputs."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            if not validate_source_generation(con, session_id, source_location_generation, source_location_revision):
                raise StaleLocationGeneration('Location consent changed')
            updated = _mark_urgent(con, session_id)
            if updated and text and text.strip():
                detail = text.strip()
                generation = capture_location_generation(con, session_id)
                revision = capture_location_revision(con, session_id)
                digest = urgent_detail_hash(detail, generation, revision)
                con.execute("""INSERT OR IGNORE INTO urgent_details (call_id,text,user_location_generation,user_location_revision)
                    SELECT id,?,?,? FROM calls WHERE id=? AND state='accepted' AND consent_version IN ('municipality-v2','municipality-v3')""",
                    (detail, generation, revision, session_id))
                con.execute("""INSERT OR IGNORE INTO urgent_detail_events (call_id,text_hash,text,user_location_generation,user_location_revision)
                    SELECT id,?,?,?,? FROM calls WHERE id=? AND state='accepted' AND consent_version IN ('municipality-v2','municipality-v3')""",
                    (digest, detail, generation, revision, session_id))
            return updated

    def submit_turn(self, session_id: str, body: TurnInput) -> tuple[Row, bool, bool] | None:
        """Atomically admit a turn and latch urgent phrases from recent caller speech."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            call = con.execute("SELECT state, urgent, ended_at FROM calls WHERE id = ?", (session_id,)).fetchone()
            if call is None or call["state"] != "accepted" or call["urgent"] or call["ended_at"]:
                return None
            if not validate_source_generation(con, session_id, body.source_location_generation, body.source_location_revision):
                raise StaleLocationGeneration('Location consent changed')
            existing = con.execute("SELECT * FROM turns WHERE session_id = ? AND request_id = ?",
                                   (session_id, body.request_id)).fetchone()
            if existing:
                if existing["input_text"] != body.text or existing["caller_context"] != body.caller_context:
                    raise DuplicateRequest(body.request_id)
                if (body.source_location_generation is not None and
                        existing['user_location_generation'] != body.source_location_generation):
                    raise DuplicateRequest(body.request_id)
                if (body.source_location_revision is not None and
                        existing['user_location_revision'] != body.source_location_revision):
                    raise DuplicateRequest(body.request_id)
                if not validate_source_generation(con, session_id, existing['user_location_generation'],
                                                  existing['user_location_revision']):
                    raise StaleLocationGeneration('Location consent changed')
                return dict(existing), False, False
            turn_id = str(uuid4())
            # Preserve the generation captured with the utterance, even if it is NULL.
            has_pending = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_utterances'").fetchone()
            pending = con.execute("SELECT * FROM pending_utterances WHERE call_id=? AND request_id=?",
                                  (session_id, body.request_id)).fetchone() if has_pending else None
            if pending is not None and (pending['input_text'] != body.text or pending['caller_context'] != body.caller_context):
                raise DuplicateRequest(body.request_id)
            if pending is None:
                generation = capture_location_generation(con, session_id)
                revision = capture_location_revision(con, session_id)
            else:
                pending_values = dict(pending)
                generation = pending_values.get('user_location_generation')
                revision = pending_values.get('user_location_revision')
                if not validate_source_generation(con, session_id, generation, revision):
                    raise StaleLocationGeneration('Location consent changed')
                if body.source_location_generation is not None and generation != body.source_location_generation:
                    raise DuplicateRequest(body.request_id)
                if body.source_location_revision is not None and revision != body.source_location_revision:
                    raise DuplicateRequest(body.request_id)
            prior = con.execute("""SELECT * FROM turns WHERE session_id = ?
                ORDER BY rowid DESC LIMIT 3""", (session_id,)).fetchall()
            def local_safety_visible(row: sqlite3.Row) -> bool:
                if user_generation_visible(con, session_id, row['user_location_generation']):
                    return True
                return bool(generation == 0 and row['user_location_generation'] == 0 and revision is not None
                            and row['user_location_revision'] == revision)
            safety_text = " ".join([str(row["input_text"])[:800] for row in reversed(prior)
                                    if local_safety_visible(row)] + [body.text])
            con.execute("""INSERT INTO turns(id, session_id, request_id, input_text, caller_context,
                        user_location_generation, user_location_revision) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (turn_id, session_id, body.request_id, body.text, body.caller_context, generation, revision))
            previous = prior[0] if prior and local_safety_visible(prior[0]) else None
            boundary_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_subject_boundaries'").fetchone()
            current_boundary = (con.execute("SELECT 1 FROM call_fact_subject_boundaries WHERE call_id=? AND utterance_id=?",
                                            (session_id, body.request_id)).fetchone() if boundary_table else None)
            guidance = last_guidance([dict(previous)]) if previous is not None and current_boundary is None else None
            unable_reply = (guidance is not None and guidance.action == 'follow_up'
                            and guidance.question_id == 'fall_walking' and walking_answer(body.text) == 'no')
            same_relative = bool(previous is not None and current_boundary is None
                                 and same_relative_with_fall(body.text, str(previous['input_text'])))
            fall_context = (has_fall(body.text) or same_relative or (current_boundary is None
                            and not mentions_other_person(body.text) and has_fall(safety_text)))
            urgent = emergency_detected(safety_text) or (fall_context and fall_urgent(body.text)) or unable_reply
            if urgent:
                _mark_urgent(con, session_id)
                message = PESTICIDE_EMERGENCY_MESSAGE if pesticide_risk(safety_text) else EMERGENCY_MESSAGE
                con.execute("""UPDATE turns SET status = 'emergency', final_text = ?, error = NULL,
                    sources_json = '[]' WHERE id = ?""", (message, turn_id))
            row = con.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            return dict(row), True, urgent

    def bind_guidance_parent(self, turn_id: str, parent_id: str) -> bool:
        """Bind the reply only while the question remains delivered and valid."""
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            parent = con.execute("""SELECT p.* FROM turns p JOIN turns child ON child.session_id=p.session_id
                WHERE p.id=? AND child.id=? AND child.status IN ('pending','processing')""",
                (parent_id, turn_id)).fetchone()
            if parent is None or visible_guidance(dict(parent)) is None or not context_turn_current(con, parent_id):
                invalidate_context_turn(con, turn_id, 'question_context_invalidated')
                return False
            con.execute('UPDATE turns SET guidance_parent_id=? WHERE id=?', (parent_id, turn_id))
            return True

    def refresh_turn_context(self, turn_id: str) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            return context_turn_current(con, turn_id)

    def update_turn_if_safe(self, turn_id: str, fields: Row) -> bool:
        if not fields or not fields.keys() <= WRITABLE:
            raise InvalidTurnFields("Invalid turn fields")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            if not context_turn_current(con, turn_id):
                return False
            cursor = con.execute(f"""UPDATE turns SET {assignments}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id = ? AND status NOT IN ('interrupted','cancelled')
                AND NOT EXISTS (SELECT 1 FROM calls WHERE calls.id = turns.session_id
                    AND (urgent = 1 OR state != 'accepted' OR ended_at IS NOT NULL))""",
                (*fields.values(), turn_id))
            return cursor.rowcount == 1

    def save_audio_if_safe(self, turn_id: str, kind: str, audio: Audio) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            if not context_turn_current(con, turn_id):
                return False
            cursor = con.execute("""INSERT INTO generated_audio (turn_id, kind, content, mime_type, spoken_text)
                SELECT id, ?, ?, ?, ? FROM turns WHERE id = ? AND status NOT IN ('interrupted','cancelled') AND NOT EXISTS
                (SELECT 1 FROM calls WHERE calls.id = turns.session_id AND (urgent = 1 OR state != 'accepted' OR ended_at IS NOT NULL))
                ON CONFLICT(turn_id, kind) DO UPDATE SET content = excluded.content, mime_type = excluded.mime_type,
                    spoken_text = excluded.spoken_text""",
                (kind, audio.data, audio.mime_type, audio.spoken_text, turn_id))
            return cursor.rowcount == 1
