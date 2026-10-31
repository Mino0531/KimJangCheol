"""Do not dispatch speech from a withdrawn or unconsented location generation."""

from hashlib import sha256
import json
import sqlite3

from app.services import EMERGENCY_MESSAGE
from app.utterance_location import user_generation_visible, validate_source_generation


REDACTED = '위치 정보가 포함될 수 있어 원문 확인 제한'


def ensure_guard_table(con: sqlite3.Connection) -> None:
    con.execute('''CREATE TABLE IF NOT EXISTS municipal_redacted_events (
        original_event_id TEXT PRIMARY KEY REFERENCES municipal_outbox(event_id),
        replacement_event_id TEXT NOT NULL)''')


def guard_interrupted(con: sqlite3.Connection) -> None:
    """Also recover speech already excluded by another cancellation path."""
    ensure_guard_table(con)
    upgrade_legacy_redactions(con)
    rows = con.execute('''SELECT o.event_id,o.call_id,o.body FROM municipal_outbox o
        JOIN calls c ON c.id=o.call_id JOIN municipal_interruptions i ON i.event_id=o.event_id
        WHERE o.state='pending' AND c.state='accepted'
        AND c.consent_version IN ('municipality-v2','municipality-v3')
        AND NOT EXISTS (SELECT 1 FROM municipal_redacted_events r WHERE r.original_event_id=o.event_id)
        AND (EXISTS (SELECT 1 FROM location_consent_events l WHERE l.call_id=o.call_id AND l.state='revoked')
          OR EXISTS (SELECT 1 FROM municipal_location_revocations v WHERE v.call_id=o.call_id)
          OR NOT EXISTS (SELECT 1 FROM call_location_consent l WHERE l.call_id=o.call_id AND l.state='accepted'))''').fetchall()
    for row in rows:
        guard_event(con, str(row['event_id']), str(row['call_id']), str(row['body']))


def upgrade_legacy_redactions(con: sqlite3.Connection) -> None:
    """Replace unsent legacy notices without changing their immutable payloads."""
    rows = con.execute('''SELECT r.original_event_id,o.event_id,o.call_id,o.body
        FROM municipal_redacted_events r JOIN municipal_outbox o ON o.event_id=r.replacement_event_id
        JOIN calls c ON c.id=o.call_id WHERE o.state='pending' AND o.event_type='turn_interrupted'
        AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')''').fetchall()
    for row in rows:
        old_id, original_id = str(row['event_id']), str(row['original_event_id'])
        con.execute('INSERT OR IGNORE INTO municipal_interruptions(event_id) VALUES (?)', (old_id,))
        try:
            previous = json.loads(str(row['body']))
        except json.JSONDecodeError:
            continue
        if not isinstance(previous, dict) or previous.get('user_text') != REDACTED:
            continue
        replacement_id = 'redacted:' + sha256(f'legacy:{original_id}'.encode('utf-8')).hexdigest()
        payload = {'source': 'hospital-ai', 'event_id': replacement_id, 'call_id': str(row['call_id']),
                   'occurred_at': previous.get('occurred_at'), 'event_type': 'privacy_redacted',
                   'phone': previous.get('phone'), 'user_text': REDACTED,
                   'assistant_text': '', 'answer_status': None, 'sources': []}
        con.execute('''INSERT OR IGNORE INTO municipal_outbox(event_id,call_id,event_type,body)
            VALUES (?,?,?,?)''', (replacement_id, str(row['call_id']), 'privacy_redacted',
            json.dumps(payload, ensure_ascii=False, separators=(',', ':'))))
        con.execute('UPDATE municipal_redacted_events SET replacement_event_id=? WHERE original_event_id=?',
                    (replacement_id, original_id))


def guard_event(con: sqlite3.Connection, event_id: str, call_id: str, body: str) -> bool:
    """Quarantine unsafe originals and atomically enqueue a safe notification."""
    ensure_guard_table(con)
    if con.execute('SELECT 1 FROM municipal_redacted_events WHERE replacement_event_id=?', (event_id,)).fetchone():
        return True
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return True  # Existing dispatcher quarantines malformed data.
    if not isinstance(payload, dict) or not payload.get('user_text'):
        return True
    if payload.get('event_id') != event_id or payload.get('call_id') != call_id:
        return True  # Existing dispatcher rejects identity mismatch.
    generation = payload.get('user_location_generation')
    revision = payload.get('user_location_revision')
    valid_generation = isinstance(generation, int) and not isinstance(generation, bool) and generation > 0
    valid_revision = revision is None or (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0)
    location = con.execute('SELECT state,generation FROM call_location_consent WHERE call_id=?', (call_id,)).fetchone()
    has_consent = location is not None and location['state'] == 'accepted'
    same_generation = valid_generation and generation == location['generation'] if has_consent else False
    if (has_consent and valid_revision and (same_generation or generation is None)
            and user_generation_visible(con, call_id, generation)
            and validate_source_generation(con, call_id, generation, revision)):
        return True
    # Never reuse the original event ID: an earlier send may already have succeeded.
    utterance_id = payload.get('utterance_id')
    identity = f'{call_id}:{utterance_id}' if isinstance(utterance_id, str) and utterance_id else event_id
    event_type = payload.get('event_type')
    urgent = event_type == 'urgent'
    replacement_id = (('redacted-urgent:' + sha256(identity.encode('utf-8')).hexdigest()) if urgent else
                      ('redacted:' + sha256(f'{identity}:privacy-v2'.encode('utf-8')).hexdigest()))
    replacement = {'source': 'hospital-ai', 'event_id': replacement_id, 'call_id': call_id,
                   'occurred_at': payload.get('occurred_at'), 'event_type': 'urgent' if urgent else 'privacy_redacted',
                   'phone': payload.get('phone'), 'user_text': '' if urgent else REDACTED,
                   'assistant_text': EMERGENCY_MESSAGE if urgent else '',
                   'answer_status': 'emergency' if urgent else None, 'sources': []}
    encoded = json.dumps(replacement, ensure_ascii=False, separators=(',', ':'))
    con.execute('''INSERT OR IGNORE INTO municipal_outbox(event_id,call_id,event_type,body)
        VALUES (?,?,?,?)''', (replacement_id, call_id, replacement['event_type'], encoded))
    con.execute('INSERT OR IGNORE INTO municipal_redacted_events(original_event_id,replacement_event_id) VALUES (?,?)',
                (event_id, replacement_id))
    con.execute('INSERT OR IGNORE INTO municipal_interruptions(event_id) VALUES (?)', (event_id,))
    return False
