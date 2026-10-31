"""One-time replay of historical interruption events rejected by older receivers."""

from contextlib import closing

from app.db import Database


def retry_legacy_interruption_once(database: Database) -> int:
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS municipal_legacy_retries (event_id TEXT PRIMARY KEY)')
        rows = con.execute("""SELECT f.event_id FROM municipal_failures f
            JOIN municipal_outbox o ON o.event_id=f.event_id
            JOIN calls c ON c.id=o.call_id
            WHERE f.status_code=422 AND o.state='pending' AND o.event_type='turn_interrupted'
            AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
            AND json_valid(o.body) AND json_extract(o.body,'$.assistant_text')='연결 오류로 응답이 중단됐습니다.'
            AND NOT EXISTS (SELECT 1 FROM municipal_legacy_retries r WHERE r.event_id=f.event_id)""").fetchall()
        for row in rows:
            con.execute('INSERT INTO municipal_legacy_retries(event_id) VALUES (?)', (row['event_id'],))
            con.execute('DELETE FROM municipal_failures WHERE event_id=?', (row['event_id'],))
        return len(rows)
