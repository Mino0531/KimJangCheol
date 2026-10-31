"""Consent state for calls in the local SQLite prototype."""

from contextlib import closing
from hashlib import sha256
import sqlite3
from typing import Literal
from uuid import uuid4

from app.audio_store import Audio
from app.db import Database, DuplicateRequest, InvalidTurnFields, Row, WRITABLE
from app.schemas import TurnInput
from app.services import EMERGENCY_MESSAGE, PESTICIDE_EMERGENCY_MESSAGE, emergency_detected, pesticide_risk

CallState = Literal["awaiting_consent", "accepted", "declined"]


def _mark_urgent(con: sqlite3.Connection, session_id: str) -> bool:
    cursor = con.execute("UPDATE calls SET urgent = 1 WHERE id = ?", (session_id,))
    if cursor.rowcount == 0:
        return False
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_cancellations'").fetchone():
        con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
            SELECT event_id FROM recommendation_outbox WHERE call_id = ? AND state = 'pending'""", (session_id,))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generated_audio'").fetchone():
        con.execute("""DELETE FROM generated_audio WHERE turn_id IN
            (SELECT id FROM turns WHERE session_id = ? AND status IN ('pending', 'processing'))""", (session_id,))
    con.execute("""UPDATE turns SET status = 'cancelled', initial_text = NULL, final_text = NULL,
        sources_json = NULL, tts_json = NULL, error = 'urgent_override',
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
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(calls)")}
            if "urgent" not in columns:
                con.execute("ALTER TABLE calls ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
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
            previous = con.execute("SELECT call_id,text,recorded_at FROM urgent_details").fetchall()
            for row in previous:
                text = str(row["text"])
                digest = sha256(text.encode("utf-8")).hexdigest()
                con.execute("""INSERT OR IGNORE INTO urgent_detail_events
                    (call_id,text_hash,text,recorded_at) VALUES (?,?,?,?)""",
                    (row["call_id"], digest, text, row["recorded_at"]))
        # New calls record location consent together with the call decision.
        from app.recommend_dashboard import RecommendationOutbox
        RecommendationOutbox(self.database).initialize()

    def create(self, session_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("INSERT INTO calls(id, consent_version) VALUES (?, ?)", (session_id, "municipality-v3"))

    def get(self, session_id: str) -> Row | None:
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM calls WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row is not None else None

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
                WHERE id = ? AND state = 'awaiting_consent' AND urgent = 0""", (state, session_id))
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

    def mark_urgent(self, session_id: str, text: str | None = None) -> bool:
        """Set urgent state and atomically suppress unfinished routine outputs."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            updated = _mark_urgent(con, session_id)
            if updated and text and text.strip():
                detail = text.strip()
                digest = sha256(detail.encode("utf-8")).hexdigest()
                con.execute("""INSERT OR IGNORE INTO urgent_details (call_id,text)
                    SELECT id,? FROM calls WHERE id=? AND state='accepted' AND consent_version IN ('municipality-v2','municipality-v3')""",
                    (detail, session_id))
                con.execute("""INSERT OR IGNORE INTO urgent_detail_events (call_id,text_hash,text)
                    SELECT id,?,? FROM calls WHERE id=? AND state='accepted' AND consent_version IN ('municipality-v2','municipality-v3')""",
                    (digest, detail, session_id))
            return updated

    def submit_turn(self, session_id: str, body: TurnInput) -> tuple[Row, bool, bool] | None:
        """Atomically admit a turn and latch urgent phrases from recent caller speech."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            call = con.execute("SELECT state, urgent FROM calls WHERE id = ?", (session_id,)).fetchone()
            if call is None or call["state"] != "accepted" or call["urgent"]:
                return None
            existing = con.execute("SELECT * FROM turns WHERE session_id = ? AND request_id = ?",
                                   (session_id, body.request_id)).fetchone()
            if existing:
                if existing["input_text"] != body.text:
                    raise DuplicateRequest(body.request_id)
                return dict(existing), False, False
            prior = con.execute("""SELECT input_text FROM turns WHERE session_id = ?
                ORDER BY rowid DESC LIMIT 3""", (session_id,)).fetchall()
            safety_text = " ".join([str(row["input_text"])[:800] for row in reversed(prior)] + [body.text])
            turn_id = str(uuid4())
            con.execute("INSERT INTO turns(id, session_id, request_id, input_text) VALUES (?, ?, ?, ?)",
                        (turn_id, session_id, body.request_id, body.text))
            urgent = emergency_detected(safety_text)
            if urgent:
                _mark_urgent(con, session_id)
                message = PESTICIDE_EMERGENCY_MESSAGE if pesticide_risk(safety_text) else EMERGENCY_MESSAGE
                con.execute("""UPDATE turns SET status = 'emergency', final_text = ?, error = NULL,
                    sources_json = '[]' WHERE id = ?""", (message, turn_id))
            row = con.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            return dict(row), True, urgent

    def update_turn_if_safe(self, turn_id: str, fields: Row) -> bool:
        if not fields or not fields.keys() <= WRITABLE:
            raise InvalidTurnFields("Invalid turn fields")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with closing(self.database.connect()) as con, con:
            cursor = con.execute(f"""UPDATE turns SET {assignments}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id = ? AND status NOT IN ('interrupted','cancelled')
                AND NOT EXISTS (SELECT 1 FROM calls WHERE calls.id = turns.session_id
                    AND (urgent = 1 OR state != 'accepted'))""",
                (*fields.values(), turn_id))
            return cursor.rowcount == 1

    def save_audio_if_safe(self, turn_id: str, kind: str, audio: Audio) -> bool:
        with closing(self.database.connect()) as con, con:
            cursor = con.execute("""INSERT INTO generated_audio (turn_id, kind, content, mime_type, spoken_text)
                SELECT id, ?, ?, ?, ? FROM turns WHERE id = ? AND status NOT IN ('interrupted','cancelled') AND NOT EXISTS
                (SELECT 1 FROM calls WHERE calls.id = turns.session_id AND (urgent = 1 OR state != 'accepted'))
                ON CONFLICT(turn_id, kind) DO UPDATE SET content = excluded.content, mime_type = excluded.mime_type,
                    spoken_text = excluded.spoken_text""",
                (kind, audio.data, audio.mime_type, audio.spoken_text, turn_id))
            return cursor.rowcount == 1
