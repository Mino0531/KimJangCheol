"""Withdraw ongoing-call consent without silently reusing prior permissions."""

from contextlib import closing

from app.db import Database
from app.recommend_dashboard import enqueue_location_revocation
from app.recommendation_turns import invalidate_location_contexts


def revoke_call(database: Database, call_id: str) -> bool:
    """Atomically stop new processing/transmission; in-flight external requests cannot be recalled."""
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        call = con.execute('SELECT state,consent_version FROM calls WHERE id=?', (call_id,)).fetchone()
        if call is None or call['state'] != 'accepted':
            return False
        con.execute("""CREATE TABLE IF NOT EXISTS call_consent_revocations (
            call_id TEXT PRIMARY KEY REFERENCES calls(id),
            revoked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        con.execute("INSERT OR IGNORE INTO call_consent_revocations(call_id) VALUES (?)", (call_id,))
        con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
        con.execute("UPDATE turns SET guidance_json=NULL,guidance_delivered=0 WHERE session_id=?", (call_id,))
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_utterances'").fetchone():
            con.execute("DELETE FROM pending_utterances WHERE call_id=?", (call_id,))
        location = con.execute('SELECT state,generation FROM call_location_consent WHERE call_id=?',
                               (call_id,)).fetchone()
        if location is not None and location['state'] == 'accepted':
            previous_generation = int(location['generation'])
            generation = previous_generation + 1
            con.execute("""UPDATE call_location_consent SET state='revoked', generation=?,
                changed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE call_id=?""", (generation, call_id))
            con.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,'revoked',?)",
                        (call_id, generation))
            invalidate_location_contexts(con, call_id)
            if previous_generation > 0 and call['consent_version'] in ('municipality-v2', 'municipality-v3'):
                enqueue_location_revocation(con, call_id, previous_generation)
        con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
            SELECT event_id FROM recommendation_outbox WHERE call_id=? AND state='pending'""", (call_id,))
        con.execute("""DELETE FROM generated_audio WHERE turn_id IN
            (SELECT id FROM turns WHERE session_id=? AND status IN ('pending','processing'))""", (call_id,))
        con.execute("""UPDATE turns SET status='cancelled', initial_text=NULL, final_text=NULL,
            sources_json=NULL, tts_json=NULL, error='consent_revoked',
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE session_id=? AND status IN ('pending','processing')""", (call_id,))
        return True
