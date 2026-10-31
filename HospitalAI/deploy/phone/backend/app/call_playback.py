"""Record browser playback completion without claiming the caller heard it."""

import json
from contextlib import closing

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox


def playback_router(database: Database, municipal: MunicipalOutbox, config: MunicipalConfig | None) -> APIRouter:
    router = APIRouter()

    @router.post('/calls/{call_id}/messages/{request_id}/playback-complete')
    def playback(call_id: str, request_id: str, background: BackgroundTasks) -> dict[str, str | bool]:
        with closing(database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("""SELECT t.id,t.status,t.tts_json,c.state,c.urgent,c.consent_version,s.phone,
                length(a.content) AS audio_length FROM turns t JOIN calls c ON c.id=t.session_id
                JOIN sessions s ON s.id=t.session_id
                LEFT JOIN generated_audio a ON a.turn_id=t.id AND a.kind='final'
                WHERE t.session_id=? AND t.request_id=?""", (call_id, request_id)).fetchone()
            if row is None:
                raise HTTPException(404, 'Turn not found')
            if row['state'] != 'accepted':
                raise HTTPException(403, 'Consent is required')
            if row['urgent'] or row['status'] not in ('completed', 'degraded'):
                raise HTTPException(409, 'Turn is not available for playback acknowledgement')
            try:
                delivery = json.loads(row['tts_json'] or '{}')
            except (json.JSONDecodeError, TypeError):
                delivery = {}
            if not isinstance(delivery, dict) or delivery.get('final') != 'synthesized' or not row['audio_length']:
                raise HTTPException(409, 'Final audio is not available')
            if row['consent_version'] not in ('municipality-v2', 'municipality-v3'):
                raise HTTPException(403, 'Municipal processing consent is required')
            event_id = f"playback:{row['id']}"
            prior = con.execute('SELECT 1 FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()
            if prior is None:
                at = con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
                event = {'source': 'hospital-ai', 'event_id': event_id, 'call_id': call_id,
                         'occurred_at': at, 'event_type': 'playback_completed', 'phone': row['phone'],
                         'user_text': '', 'assistant_text': '', 'answer_status': None, 'sources': []}
                municipal._insert(con, event)
            if config is not None:
                background.add_task(municipal.dispatch, config)
            return {'status': 'playback_completed', 'duplicate': prior is not None}

    return router
