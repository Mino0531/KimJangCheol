"""Consent-gated admission of caller speech before intent classification."""

import sqlite3
from contextlib import closing
from hashlib import sha256
from uuid import uuid4

from app.call_facts import mark_patient_subject_change
from app.db import Database, DuplicateRequest
from app.municipal_bridge import MunicipalOutbox
from app.utterance_location import (
    capture_location_generation,
    capture_location_revision,
    validate_source_generation,
)


class PendingUtterances:
    """Store one ordered, idempotent utterance per accepted call."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS pending_utterances (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL REFERENCES calls(id),
                request_id TEXT NOT NULL,
                input_text TEXT NOT NULL,
                caller_context TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','accepted')),
                intent TEXT,
                needs_response INTEGER NOT NULL DEFAULT 1 CHECK(needs_response IN (0,1)),
                user_location_generation INTEGER,
                user_location_revision INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(call_id,request_id))""")
            columns = {row[1] for row in con.execute('PRAGMA table_info(pending_utterances)')}
            if 'needs_response' not in columns:
                con.execute('ALTER TABLE pending_utterances ADD COLUMN needs_response INTEGER NOT NULL DEFAULT 1')
            if 'user_location_generation' not in columns:
                con.execute('ALTER TABLE pending_utterances ADD COLUMN user_location_generation INTEGER')
            if 'user_location_revision' not in columns:
                con.execute('ALTER TABLE pending_utterances ADD COLUMN user_location_revision INTEGER')

    def admit(self, call_id: str, request_id: str, text: str, caller_context: str,
              source_generation: int | None = None, source_revision: int | None = None) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            call = con.execute('SELECT state,urgent,ended_at FROM calls WHERE id=?', (call_id,)).fetchone()
            if call is None or call['state'] != 'accepted' or call['urgent'] or call['ended_at']:
                return False
            if not validate_source_generation(con, call_id, source_generation, source_revision):
                return False
            previous = con.execute('''SELECT input_text,caller_context,user_location_generation,user_location_revision
                FROM pending_utterances WHERE call_id=? AND request_id=?''',
                                   (call_id, request_id)).fetchone()
            if previous is not None:
                if previous['input_text'] != text or previous['caller_context'] != caller_context:
                    raise DuplicateRequest(request_id)
                return validate_source_generation(con, call_id, previous['user_location_generation'], previous['user_location_revision'])
            generation = capture_location_generation(con, call_id)
            revision = capture_location_revision(con, call_id)
            con.execute("""INSERT INTO pending_utterances
                (call_id,request_id,input_text,caller_context,state,user_location_generation,user_location_revision)
                VALUES (?,?,?,?,'pending',?,?)""", (call_id, request_id, text, caller_context, generation, revision))
            return True

    def classified(self, call_id: str, request_id: str, intent: str, expects_turn: bool = True,
                   proxy_subject: bool = False, patient_changed: bool = False) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            call = con.execute('SELECT state,urgent,ended_at,consent_version FROM calls WHERE id=?', (call_id,)).fetchone()
            if call is None or call['state'] != 'accepted' or call['urgent']:
                con.execute('DELETE FROM pending_utterances WHERE call_id=? AND request_id=?', (call_id, request_id))
                return False
            source = con.execute("""SELECT user_location_generation,user_location_revision FROM pending_utterances
                WHERE call_id=? AND request_id=?""", (call_id, request_id)).fetchone()
            if source is None or not validate_source_generation(con, call_id, source['user_location_generation'], source['user_location_revision']):
                con.execute('DELETE FROM pending_utterances WHERE call_id=? AND request_id=?', (call_id, request_id))
                return False
            if intent in ('revoke_consent', 'revoke_location'):
                con.execute('DELETE FROM pending_utterances WHERE call_id=? AND request_id=?', (call_id, request_id))
                return True
            mark_boundary = patient_changed
            if proxy_subject or patient_changed:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_subject_boundaries'").fetchone()
                if not exists:
                    return False
                if proxy_subject and not con.execute('SELECT 1 FROM call_fact_subject_boundaries WHERE call_id=? LIMIT 1', (call_id,)).fetchone():
                    mark_boundary = True
            con.execute('SAVEPOINT patient_subject')
            con.execute("""UPDATE pending_utterances SET state='accepted',intent=?,needs_response=?
                WHERE call_id=? AND request_id=? AND state='pending'""", (intent, int(expects_turn), call_id, request_id))
            if mark_boundary and not mark_patient_subject_change(con, call_id, request_id):
                con.execute('ROLLBACK TO SAVEPOINT patient_subject')
                con.execute('RELEASE SAVEPOINT patient_subject')
                return False
            con.execute('RELEASE SAVEPOINT patient_subject')
            if intent not in ('consultation', 'recommend'):
                con.execute('DELETE FROM pending_utterances WHERE call_id=? AND request_id=?', (call_id, request_id))
                return True
            if call['consent_version'] in ('municipality-v2', 'municipality-v3'):
                self._enqueue_utterance(con, call_id, request_id)
            if call['ended_at']:
                self.flush_ended(con, call_id)
            return True

    @staticmethod
    def _enqueue_utterance(con: sqlite3.Connection, call_id: str, request_id: str) -> None:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipal_outbox'").fetchone():
            return
        row = con.execute("""SELECT u.input_text,u.created_at,u.user_location_generation,s.phone FROM pending_utterances u
            JOIN sessions s ON s.id=u.call_id WHERE u.call_id=? AND u.request_id=? AND u.state='accepted'""",
            (call_id, request_id)).fetchone()
        if row is None:
            return
        event_id = 'utterance:' + sha256(f'{call_id}:{request_id}'.encode()).hexdigest()
        MunicipalOutbox._insert(con, {'source': 'hospital-ai', 'event_id': event_id, 'call_id': call_id,
            'occurred_at': row['created_at'], 'event_type': 'caller_utterance', 'phone': row['phone'],
            'utterance_id': request_id, 'user_text': row['input_text'], 'assistant_text': '',
            'answer_status': None, 'sources': [], 'user_location_generation': row['user_location_generation']})

    @staticmethod
    def flush_ended(con: sqlite3.Connection, call_id: str) -> None:
        """Recover safely classified consultations that never became turns."""
        call = con.execute('SELECT state,urgent,ended_at FROM calls WHERE id=?', (call_id,)).fetchone()
        if call is None or call['state'] != 'accepted' or call['urgent'] or not call['ended_at']:
            return
        rows = con.execute("""SELECT request_id,input_text,caller_context,created_at,user_location_generation,user_location_revision FROM pending_utterances u
            WHERE u.call_id=? AND u.state='accepted' AND u.intent='consultation' AND u.needs_response=1
            AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=u.call_id AND t.request_id=u.request_id)
            ORDER BY u.seq""", (call_id,)).fetchall()
        has_outbox = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipal_outbox'").fetchone()
        for row in rows:
            if not validate_source_generation(con, call_id, row['user_location_generation'], row['user_location_revision']):
                continue
            con.execute("""INSERT INTO turns(id,session_id,request_id,input_text,caller_context,status,error,created_at,user_location_generation,user_location_revision)
                VALUES (?,?,?,?,?,'cancelled','call_ended',?,?,?)""", (str(uuid4()), call_id, row['request_id'],
                row['input_text'], row['caller_context'], row['created_at'], row['user_location_generation'], row['user_location_revision']))
            if has_outbox:
                MunicipalOutbox.insert_interrupted(con, call_id, str(row['request_id']))
