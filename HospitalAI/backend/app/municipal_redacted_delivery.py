"""Capability-gated delivery for privacy-redacted notices."""

from contextlib import closing
import json
import logging
import socket
from typing import Literal

import anyio
import httpx2
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.db import Database
from app.hospital_bridge import DeliveryAck, DispatchResult
from app.municipal_bridge import MunicipalConfig
from app.municipal_raw_guard import REDACTED

logger = logging.getLogger(__name__)


class RedactionCapability(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    privacy_redacted: StrictBool


class RedactedNotice(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['hospital-ai']
    event_id: str = Field(pattern=r'^redacted:[0-9a-f]{64}$')
    call_id: str = Field(min_length=1, max_length=128)
    occurred_at: AwareDatetime
    event_type: Literal['privacy_redacted']
    phone: str = Field(min_length=1, max_length=32)
    user_text: Literal[REDACTED]
    assistant_text: Literal['']
    answer_status: None
    sources: list[None] = Field(max_length=0)


def _has_table(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _pending(database: Database) -> int:
    with closing(database.connect()) as con:
        if not _has_table(con, 'municipal_redacted_events'):
            return 0
        return int(con.execute('''SELECT count(*) FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
            WHERE o.state='pending' AND o.event_type='privacy_redacted' AND c.state='accepted'
              AND c.consent_version IN ('municipality-v2','municipality-v3')
              AND EXISTS (SELECT 1 FROM municipal_redacted_events r WHERE r.replacement_event_id=o.event_id)
              AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
              AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)''').fetchone()[0])


def _next(database: Database, event_id: str | None = None) -> tuple[str, str, str] | None:
    with closing(database.connect()) as con:
        if not _has_table(con, 'municipal_redacted_events'):
            return None
        row = con.execute('''SELECT o.event_id,o.call_id,o.body FROM municipal_outbox o
            JOIN calls c ON c.id=o.call_id WHERE o.state='pending' AND o.event_type='privacy_redacted'
              AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
              AND (? IS NULL OR o.event_id=?)
              AND EXISTS (SELECT 1 FROM municipal_redacted_events r WHERE r.replacement_event_id=o.event_id)
              AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
              AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
              AND NOT EXISTS (SELECT 1 FROM municipal_interpretation_gates g
                              WHERE g.call_id=o.call_id AND g.expires_at > unixepoch('now'))
              AND (c.urgent=0 OR EXISTS (SELECT 1 FROM municipal_outbox u
                   WHERE u.call_id=c.id AND u.event_type='urgent' AND u.state='sent'))
            ORDER BY o.rowid LIMIT 1''', (event_id, event_id)).fetchone()
        return (str(row[0]), str(row[1]), str(row[2])) if row else None


def _mark_sent(database: Database, event_id: str, call_id: str, body: str) -> bool:
    with closing(database.connect()) as con, con:
        result = con.execute('''UPDATE municipal_outbox SET state='sent'
            WHERE event_id=? AND call_id=? AND body=? AND state='pending'
            AND EXISTS (SELECT 1 FROM calls c WHERE c.id=municipal_outbox.call_id AND c.state='accepted'
                        AND c.consent_version IN ('municipality-v2','municipality-v3'))''',
            (event_id, call_id, body))
        return result.rowcount == 1


def _mark_invalid(database: Database, event_id: str) -> None:
    with closing(database.connect()) as con, con:
        con.execute('INSERT OR IGNORE INTO municipal_failures(event_id,status_code) VALUES (?,422)', (event_id,))


async def dispatch_redacted(database: Database, config: MunicipalConfig) -> DispatchResult:
    """Send only after this authenticated receiver advertises privacy-redacted support."""
    pending = await anyio.to_thread.run_sync(_pending, database)
    if not pending:
        return DispatchResult(0, 0)
    limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30.0)
    transport = httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
    headers = {'Authorization': f'Bearer {config.token}', 'Accept': 'application/json'}
    delivered = 0
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=timeout,
                                      follow_redirects=False, trust_env=False) as client:
            capability = await client.get(f'{config.base_url}/api/municipality/intake/capabilities', headers=headers)
            if capability.status_code != 200 or not RedactionCapability.model_validate(capability.json()).privacy_redacted:
                return DispatchResult(0, await anyio.to_thread.run_sync(_pending, database))
            while item := await anyio.to_thread.run_sync(_next, database):
                event_id, call_id, body = item
                try:
                    notice = RedactedNotice.model_validate_json(body)
                except ValidationError:
                    logger.warning('Invalid privacy notice retained as failed')
                    await anyio.to_thread.run_sync(_mark_invalid, database, event_id)
                    continue
                if notice.event_id != event_id or notice.call_id != call_id:
                    await anyio.to_thread.run_sync(_mark_invalid, database, event_id)
                    continue
                if await anyio.to_thread.run_sync(_next, database, event_id) != item:
                    continue
                response = await client.post(f'{config.base_url}/api/municipality/intake',
                    headers={**headers, 'Content-Type': 'application/json'}, content=body.encode('utf-8'))
                if response.status_code not in (200, 201):
                    break
                ack = DeliveryAck.model_validate(response.json())
                created = response.status_code == 201 and ack.applied and not ack.duplicate
                replay = response.status_code == 200 and not ack.applied and ack.duplicate
                if not (created or replay) or ack.event_id != event_id or ack.call_id != call_id:
                    break
                if await anyio.to_thread.run_sync(_mark_sent, database, event_id, call_id, body):
                    delivered += 1
    except (httpx2.HTTPError, ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning('Privacy notice delivery unavailable; pending events retained')
    return DispatchResult(delivered, await anyio.to_thread.run_sync(_pending, database))
