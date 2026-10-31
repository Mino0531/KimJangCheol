"""Stop an answer that the caller interrupted without losing their question."""

from contextlib import closing
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict

from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox


class InterruptInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    reason: Literal['barge_in', 'service_error'] = 'barge_in'


def interrupt_router(database: Database, municipal: MunicipalOutbox, config: MunicipalConfig | None) -> APIRouter:
    router = APIRouter()

    @router.post('/calls/{call_id}/messages/{request_id}/interrupt')
    def interrupt(call_id: str, request_id: str, background: BackgroundTasks,
                  body: InterruptInput = InterruptInput()) -> dict[str, str | bool]:
        with closing(database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("""SELECT t.id,t.status,c.state,c.urgent,c.ended_at
                FROM turns t JOIN calls c ON c.id=t.session_id
                WHERE t.session_id=? AND t.request_id=?""", (call_id, request_id)).fetchone()
            if row is None:
                raise HTTPException(404, 'Turn not found')
            if row['state'] != 'accepted':
                raise HTTPException(403, 'Consent is required')
            if row['ended_at']:
                raise HTTPException(409, 'Call has ended')
            if row['urgent'] or row['status'] in ('cancelled', 'emergency'):
                raise HTTPException(409, 'Urgent or cancelled turn')
            if row['status'] not in ('pending', 'processing', 'completed', 'degraded', 'interrupted'):
                raise HTTPException(409, 'Turn cannot be interrupted')
            event_id = f"turn:{row['id']}"
            # Pending delivery can already have started; its remote outcome is unknown.
            event = con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()
            if row['status'] != 'interrupted':
                con.execute("""UPDATE turns SET status='interrupted',initial_text=NULL,final_text=NULL,
                    sources_json=NULL,tts_json=NULL,guidance_json=NULL,guidance_delivered=0,error=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""", (body.reason, row['id']))
                con.execute('DELETE FROM generated_audio WHERE turn_id=?', (row['id'],))
                if event is not None and event['state'] == 'pending':
                    con.execute('INSERT OR IGNORE INTO municipal_interruptions(event_id) VALUES (?)', (event_id,))
            municipal.insert_interrupted(con, call_id, request_id)
            if config is not None:
                background.add_task(municipal.dispatch, config)
            return {'status': 'interrupted', 'delivery_may_have_occurred': event is not None}

    return router
