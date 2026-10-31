"""Stop an answer that the caller interrupted without losing their question."""

from contextlib import closing

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox


def interrupt_router(database: Database, municipal: MunicipalOutbox, config: MunicipalConfig | None) -> APIRouter:
    router = APIRouter()

    @router.post('/calls/{call_id}/messages/{request_id}/interrupt')
    def interrupt(call_id: str, request_id: str, background: BackgroundTasks) -> dict[str, str | bool]:
        with closing(database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("""SELECT t.id,t.status,c.state,c.urgent
                FROM turns t JOIN calls c ON c.id=t.session_id
                WHERE t.session_id=? AND t.request_id=?""", (call_id, request_id)).fetchone()
            if row is None:
                raise HTTPException(404, 'Turn not found')
            if row['state'] != 'accepted':
                raise HTTPException(403, 'Consent is required')
            if row['urgent'] or row['status'] in ('cancelled', 'emergency'):
                raise HTTPException(409, 'Urgent or cancelled turn')
            if row['status'] not in ('pending', 'processing', 'completed', 'degraded', 'interrupted'):
                raise HTTPException(409, 'Turn cannot be interrupted')
            event_id = f"turn:{row['id']}"
            # Pending delivery can already have started; its remote outcome is unknown.
            event = con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()
            if row['status'] != 'interrupted':
                con.execute("""UPDATE turns SET status='interrupted',initial_text=NULL,final_text=NULL,
                    sources_json=NULL,tts_json=NULL,error='barge_in',
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""", (row['id'],))
                con.execute('DELETE FROM generated_audio WHERE turn_id=?', (row['id'],))
                if event is not None and event['state'] == 'pending':
                    con.execute('INSERT OR IGNORE INTO municipal_interruptions(event_id) VALUES (?)', (event_id,))
            municipal.insert_interrupted(con, call_id, request_id)
            if config is not None:
                background.add_task(municipal.dispatch, config)
            return {'status': 'interrupted', 'delivery_may_have_occurred': event is not None}

    return router
