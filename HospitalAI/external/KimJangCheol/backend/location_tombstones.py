"""Withdrawal guards shared by intake, handoff, and hospital read paths.

Callers MUST use the same transaction for admission and withdrawal and apply
redaction on every read path. Stored events remain immutable for idempotency.
"""

import json
from collections.abc import MutableMapping

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session


class InvalidLocationEvent(ValueError):
    """A location event cannot be linked to a valid call or generation."""


def initialize_location_tombstones(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql('''CREATE TABLE IF NOT EXISTS municipal_location_tombstones (
            source TEXT NOT NULL, call_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation > 0),
            PRIMARY KEY(source, call_id))''')


def record_location_revocation(session: Session, source: str, call_id: str, generation: int) -> None:
    """Record the highest withdrawn generation inside the caller's transaction."""
    if isinstance(generation, bool) or generation < 1:
        raise InvalidLocationEvent('Invalid location generation')
    session.execute(text('''INSERT INTO municipal_location_tombstones(source,call_id,generation)
        VALUES (:source,:call_id,:generation)
        ON CONFLICT(source,call_id) DO UPDATE SET generation=MAX(generation,excluded.generation)'''),
        {'source': source, 'call_id': call_id, 'generation': generation})


def withdrawn_generation(session: Session, source: str, call_id: str) -> int:
    row = session.execute(text('''SELECT generation FROM municipal_location_tombstones
        WHERE source=:source AND call_id=:call_id'''), {'source': source, 'call_id': call_id}).first()
    return int(row[0]) if row else 0


def _references_valid(session: Session, source: str, call_id: str,
                      generations: list[int], event_ids: list[str]) -> bool:
    """Verify each source event actually arrived for this call and generation."""
    if not event_ids or not generations:
        return False
    present = session.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipal_events'"))
    if present.first() is None:
        return False
    seen: set[int] = set()
    for event_id in event_ids:
        if not isinstance(event_id, str) or not event_id:
            return False
        row = session.execute(text('''SELECT data FROM municipal_events WHERE source=:source
            AND call_id=:call_id AND event_id=:event_id AND event_type='recommendation' '''),
            {'source': source, 'call_id': call_id, 'event_id': event_id}).first()
        if row is None:
            return False
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        generation = payload.get('location_generation')
        if not isinstance(generation, int) or isinstance(generation, bool) or generation not in generations:
            return False
        seen.add(generation)
    return seen == set(generations)


def redact_location_event(session: Session, event: MutableMapping[str, JsonValue]) -> MutableMapping[str, JsonValue]:
    """Keep caller speech while suppressing withdrawn or untraceable derived data."""
    result = dict(event)
    source, call_id = event.get('source'), event.get('call_id')
    if not isinstance(source, str) or not isinstance(call_id, str):
        raise InvalidLocationEvent('Event identity missing')
    revoked = withdrawn_generation(session, source, call_id)
    if not revoked:
        return result
    user_text = event.get('user_text')
    user_generation = event.get('user_location_generation')
    if isinstance(user_text, str) and user_text.strip() and (
            not isinstance(user_generation, int) or isinstance(user_generation, bool)
            or user_generation < 1 or user_generation <= revoked):
        result['user_text'] = '위치 정보가 포함될 수 있어 원문 확인 제한'
        result['location_withdrawn'] = True
    if event.get('event_type') == 'recommendation':
        generation = event.get('location_generation')
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= revoked:
            result['recommendation'] = None
            result['location_withdrawn'] = True
        return result
    generations = event.get('location_generations')
    unsafe = generations is None or not isinstance(generations, list)
    if isinstance(generations, list):
        unsafe = any(not isinstance(value, int) or isinstance(value, bool) or value < 1 or value <= revoked
                     for value in generations)
        sources = event.get('sources')
        if not generations and isinstance(sources, list):
            unsafe = any(isinstance(ref, dict) and ref.get('id') == 'clinic:recent-recommendation'
                         for ref in sources)
        if not generations and event.get('recommendation_event_ids'):
            unsafe = True
        if generations and not unsafe:
            event_ids = event.get('recommendation_event_ids')
            unsafe = not isinstance(event_ids, list) or not _references_valid(
                session, source, call_id, generations, event_ids)
    if unsafe and event.get('assistant_text'):
        result['assistant_text'] = ''
        result['sources'] = []
        result['location_withdrawn'] = True
    return result
