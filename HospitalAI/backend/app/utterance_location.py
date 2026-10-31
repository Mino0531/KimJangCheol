"""Scope caller-supplied text to the location consent held when it arrived."""

import sqlite3
from hashlib import sha256


class StaleLocationGeneration(Exception):
    """Caller text originated in an obsolete or unknown consent generation."""


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def capture_location_generation(con: sqlite3.Connection, call_id: str) -> int:
    """Capture consent under the caller's existing write transaction; zero means none."""
    if not _has_table(con, 'call_location_consent'):
        return 0
    row = con.execute("SELECT generation FROM call_location_consent WHERE call_id=? AND state='accepted'", (call_id,)).fetchone()
    return int(row[0]) if row is not None else 0


def capture_location_revision(con: sqlite3.Connection, call_id: str) -> int:
    """Capture the latest consent-event identity in the current transaction."""
    if not _has_table(con, 'location_consent_events'):
        return 0
    row = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def urgent_detail_hash(text: str, generation: int | None, revision: int | None = None) -> str:
    """Keep legacy hashes stable while deduplicating new details per generation."""
    if generation is None:
        encoded = text.encode()
    elif revision is None:
        encoded = f'{generation}\0{text}'.encode()
    else:
        encoded = f'{generation}\0{revision}\0{text}'.encode()
    return sha256(encoded).hexdigest()


def validate_source_generation(con: sqlite3.Connection, call_id: str, source_generation: int | None,
                               source_revision: int | None = None) -> bool:
    """Reject stale requests; legacy requests cannot pass after a location boundary."""
    has_location = _has_table(con, 'call_location_consent')
    has_events = _has_table(con, 'location_consent_events')
    if has_location and has_events:
        snapshot = con.execute("""SELECT
            COALESCE((SELECT generation FROM call_location_consent WHERE call_id=? AND state='accepted'),0),
            COALESCE((SELECT max(id) FROM location_consent_events WHERE call_id=?),0),
            (SELECT count(*) FROM location_consent_events WHERE call_id=? AND state='revoked')""",
            (call_id, call_id, call_id)).fetchone()
        current_generation, current_revision, revocations = map(int, snapshot)
    else:
        current_generation = capture_location_generation(con, call_id)
        current_revision = capture_location_revision(con, call_id)
        revocations = (int(con.execute("SELECT count(*) FROM location_consent_events WHERE call_id=? AND state='revoked'",
                                       (call_id,)).fetchone()[0]) if has_events else 0)
    if source_revision is not None and source_revision != current_revision:
        return False
    if source_generation is not None:
        if source_generation == 0 and source_revision is None and revocations:
            return False
        return source_generation == current_generation
    if not user_generation_visible(con, call_id, None):
        return False
    if _has_table(con, 'call_location_resets'):
        return con.execute('SELECT 1 FROM call_location_resets WHERE call_id=? LIMIT 1', (call_id,)).fetchone() is None
    return True


def user_generation_visible(con: sqlite3.Connection, call_id: str, generation: int | None) -> bool:
    """Never reuse raw text from an invalidated or unknowable generation after withdrawal."""
    latest_revocation = None
    if _has_table(con, 'location_consent_events'):
        latest_revocation = con.execute("""SELECT max(generation) FROM location_consent_events
            WHERE call_id=? AND state='revoked'""", (call_id,)).fetchone()[0]
    if _has_table(con, 'municipal_location_revocations'):
        notified = con.execute("SELECT max(generation) FROM municipal_location_revocations WHERE call_id=?", (call_id,)).fetchone()[0]
        if notified is not None:
            latest_revocation = max(int(latest_revocation or 0), int(notified))
    if latest_revocation is None:
        return True
    return generation is not None and generation > int(latest_revocation)
