"""End active call processing while retaining previously consented records."""

from collections.abc import Callable
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.db import Database
from app.municipal_bridge import MunicipalOutbox

EndReason = Literal["caller", "hangup", "connection_lost", "safety_stop", "consent_declined"]


class EndCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: EndReason


class EndCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["ended"] = "ended"
    reason: EndReason
    ended_at: str


def end_call(database: Database, call_id: str, reason: EndReason) -> EndCallResult | None:
    with closing(database.connect()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT ended_at, end_reason FROM calls WHERE id = ?", (call_id,)).fetchone()
        if row is None:
            return None
        if row["ended_at"] is None:
            con.execute("""UPDATE calls SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                end_reason = ? WHERE id = ?""", (reason, call_id))
            con.execute("""DELETE FROM generated_audio WHERE turn_id IN
                (SELECT id FROM turns WHERE session_id = ? AND status IN ('pending', 'processing'))""", (call_id,))
            con.execute("""UPDATE turns SET status = 'cancelled', initial_text = NULL, final_text = NULL,
                sources_json = NULL, tts_json = NULL, error = 'call_ended',
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE session_id = ? AND status IN ('pending', 'processing')""", (call_id,))
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_utterances'").fetchone():
                from app.call_utterances import PendingUtterances
                PendingUtterances.flush_ended(con, call_id)
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='municipal_outbox'").fetchone():
                pending = con.execute("""SELECT request_id FROM turns WHERE session_id=?
                    AND status='cancelled' AND error='call_ended'""", (call_id,)).fetchall()
                for turn in pending:
                    MunicipalOutbox.insert_interrupted(con, call_id, str(turn['request_id']))
                MunicipalOutbox.insert_ended(con, call_id)
            row = con.execute("SELECT ended_at, end_reason FROM calls WHERE id = ?", (call_id,)).fetchone()
        return EndCallResult(reason=row["end_reason"], ended_at=row["ended_at"])


def call_end_router(database: Database, auth: Callable[..., None]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth)])

    @router.post("/calls/{call_id}/end")
    def end(call_id: str, body: EndCallInput) -> EndCallResult:
        result = end_call(database, call_id, body.reason)
        if result is None:
            raise HTTPException(404, "Call not found")
        return result

    return router
