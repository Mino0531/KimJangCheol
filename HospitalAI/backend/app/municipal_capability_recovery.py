"""Recover an unsupported caller-utterance event once the receiver supports it."""

from contextlib import closing
import json
import socket

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from app.db import Database
from app.municipal_bridge import MunicipalConfig


class ReceiverCapabilities(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    caller_utterance: StrictBool


def _recover(database: Database, *, apply: bool) -> int:
    """Recheck every eligibility condition in the transaction that releases failures."""
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('''CREATE TABLE IF NOT EXISTS municipal_utterance_capability_retries (
            event_id TEXT PRIMARY KEY REFERENCES municipal_outbox(event_id))''')
        rows = con.execute('''SELECT f.event_id FROM municipal_failures f
            JOIN municipal_outbox o ON o.event_id=f.event_id
            JOIN calls c ON c.id=o.call_id
            WHERE f.status_code=422 AND o.state='pending' AND o.event_type='caller_utterance'
            AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
            AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
            AND NOT EXISTS (SELECT 1 FROM municipal_utterance_capability_retries r WHERE r.event_id=o.event_id)
            ORDER BY o.rowid''').fetchall()
        if not apply:
            return len(rows)
        for row in rows:
            con.execute('INSERT INTO municipal_utterance_capability_retries(event_id) VALUES (?)', (row['event_id'],))
            con.execute('DELETE FROM municipal_failures WHERE event_id=?', (row['event_id'],))
        return len(rows)


async def recover_failed_utterances(database: Database, config: MunicipalConfig) -> int:
    """Release one retry only after this authenticated receiver confirms compatibility."""
    if not await anyio.to_thread.run_sync(lambda: _recover(database, apply=False)):
        return 0
    limits = httpx2.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
    transport = httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    timeout = httpx2.Timeout(connect=5, read=5, write=5, pool=5)
    try:
        async with httpx2.AsyncClient(transport=transport, timeout=timeout,
                                      follow_redirects=False, trust_env=False) as client:
            response = await client.get(f"{config.base_url.rstrip('/')}/api/municipality/intake/capabilities",
                headers={'Authorization': f'Bearer {config.token}', 'Accept': 'application/json'})
        if response.status_code != 200:
            return 0
        capabilities = ReceiverCapabilities.model_validate(response.json())
        if capabilities.caller_utterance is not True:
            return 0
    except (httpx2.HTTPError, ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    return await anyio.to_thread.run_sync(lambda: _recover(database, apply=True))
