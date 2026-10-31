"""Per-call speech preferences, separate from shared provider configuration."""

from collections.abc import Callable
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app.db import Database
from app.elevenlabs import ElevenLabsTTS
from app.services import TTS, TTSResult

SpeechSpeed = Literal["slow", "normal"]


class SpeechSpeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    speed: SpeechSpeed


def initialize_speech_speed(db: Database) -> None:
    with closing(db.connect()) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS call_speech_speed (
            call_id TEXT PRIMARY KEY REFERENCES calls(id),
            speed TEXT NOT NULL CHECK(speed IN ('slow', 'normal'))
        )""")


def call_speech_speed(db: Database, call_id: str) -> float:
    """Read an active call's preference; legacy databases default to normal."""
    with closing(db.connect()) as con:
        table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_speech_speed'").fetchone()
        if table is None:
            return 1.0
        row = con.execute("""SELECT s.speed FROM call_speech_speed s JOIN calls c ON c.id=s.call_id
            WHERE c.id=? AND c.state='accepted' AND c.ended_at IS NULL""", (call_id,)).fetchone()
    return 0.85 if row is not None and row['speed'] == 'slow' else 1.0


async def deliver_at_speed(tts: TTS, text: str, kind: str, speed: float) -> TTSResult:
    """Use per-request settings for ElevenLabs; preserve existing mock adapters."""
    if isinstance(tts, ElevenLabsTTS):
        return await tts.deliver(text, kind, speed=speed)
    return await tts.deliver(text, kind)


def call_speed_router(db: Database, auth: Callable[..., None]) -> APIRouter:
    initialize_speech_speed(db)
    router = APIRouter(dependencies=[Depends(auth)])

    @router.post('/calls/{call_id}/speech-speed')
    def update_speed(call_id: str, body: SpeechSpeedInput) -> SpeechSpeedInput:
        with closing(db.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            cursor = con.execute("""INSERT INTO call_speech_speed(call_id,speed)
                SELECT id,? FROM calls WHERE id=? AND state='accepted' AND ended_at IS NULL
                ON CONFLICT(call_id) DO UPDATE SET speed=excluded.speed""", (body.speed, call_id))
            if cursor.rowcount != 1:
                raise HTTPException(409, 'Call is not active and consented')
        return body

    return router
