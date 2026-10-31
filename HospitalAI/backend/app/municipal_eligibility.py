"""SQLite admission rules for pending municipal delivery."""

from contextlib import closing

from app.db import Database
from app.municipal_raw_guard import guard_event, guard_interrupted
from app.recommendation_turns import municipal_context_current


def next_municipal_event(database: Database) -> tuple[str, str, str] | None:
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        guard_interrupted(con)
        while True:
            row = con.execute("""SELECT o.event_id,o.call_id,o.body FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
            WHERE o.state='pending' AND o.event_type!='privacy_redacted' AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
            AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
            AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
            AND (o.event_type='urgent' OR NOT EXISTS (SELECT 1 FROM municipal_interpretation_gates g
                WHERE g.call_id=o.call_id AND g.expires_at > unixepoch('now')))
            AND (c.urgent=0 OR o.event_type='urgent' OR EXISTS (
                SELECT 1 FROM municipal_outbox u WHERE u.call_id=c.id AND u.event_type='urgent' AND u.state='sent'))
            ORDER BY CASE o.event_type WHEN 'urgent' THEN 0 ELSE 1 END,o.rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            event_id, call_id, body = (str(row["event_id"]), str(row["call_id"]), str(row["body"]))
            if guard_event(con, event_id, call_id, body):
                return event_id, call_id, body


def municipal_event_eligible(database: Database, event_id: str, call_id: str) -> bool:
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        eligible = con.execute("""SELECT o.body FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
            WHERE o.event_id=? AND o.call_id=? AND o.state='pending' AND o.event_type!='privacy_redacted' AND c.state='accepted'
            AND c.consent_version IN ('municipality-v2','municipality-v3')
            AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
            AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
            AND (o.event_type='urgent' OR NOT EXISTS (SELECT 1 FROM municipal_interpretation_gates g
                WHERE g.call_id=o.call_id AND g.expires_at > unixepoch('now')))
            AND (c.urgent=0 OR o.event_type='urgent' OR EXISTS (
                SELECT 1 FROM municipal_outbox u WHERE u.call_id=c.id AND u.event_type='urgent' AND u.state='sent'))""",
            (event_id, call_id)).fetchone()
        return (eligible is not None and guard_event(con, event_id, call_id, str(eligible['body']))
                and municipal_context_current(con, event_id))
