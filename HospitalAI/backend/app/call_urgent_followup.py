"""Consent-scoped emergency follow-up with only fixed, nonclinical speech."""

import base64
from collections.abc import Awaitable, Callable
from contextlib import closing
from functools import partial
from typing import Literal
from uuid import uuid4

import anyio
import httpx2
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.call_end import end_call
from app.call_interpret import LowProvider
from app.call_revocation import revoke_call
from app.call_speed import call_speech_speed, deliver_at_speed
from app.call_store import CallStore
from app.db import Database, Row
from app.orchestrator import SpeechOutput
from app.recommend_dashboard import RecommendationOutbox
from app.services import ServiceError
from app.urgent_intent import UrgentHistory, UrgentIntent, classify_urgent
from app.utterance_location import (
    capture_location_generation,
    capture_location_revision,
    urgent_detail_hash,
    user_generation_visible,
    validate_source_generation,
)

UrgentPrompt = Literal['urgent_help', 'urgent_connected', 'urgent_support', 'urgent_nearby', 'urgent_goodbye', 'urgent_revoked', 'urgent_location_revoked']
PROMPTS: dict[UrgentPrompt, str] = {
    'urgent_help': '지금 119에 연락해 주세요. 직접 어렵다면 곁에 있는 분께 부탁해 주세요. 연락하실 수 있나요?',
    'urgent_connected': '119에 연락하셨군요. 연결되면 상담원의 안내를 따라 주세요. 곁에 도와주실 분이 있나요?',
    'urgent_support': '곁에 있는 분께 지금 119에 연락해 달라고 부탁해 주세요. 도움을 청할 분이 있나요?',
    'urgent_nearby': '곁에 계신 분께 지금 119에 연락해 달라고 부탁해 주세요.',
    'urgent_goodbye': '네, 통화를 마칠게요. 아직 119에 연락하지 못했다면 바로 도움을 요청해 주세요.',
    'urgent_revoked': '동의를 철회하고 상담을 마칠게요. 이미 전달된 내용은 자동으로 회수되지 않습니다.',
    'urgent_location_revoked': '위치 정보 사용을 멈출게요. 지금 도움을 요청해 주세요.',
}


class UrgentFollowupInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    source_location_generation: int | None = Field(default=None, ge=0, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)


class UrgentReply(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    action: Literal['continue', 'end', 'revoke', 'revoke_location']
    prompt_kind: UrgentPrompt
    prompt: str
    location_generation: int | None = None
    location_revision: int | None = None


def _reply(intent: UrgentIntent) -> UrgentReply:
    kinds: dict[UrgentIntent, UrgentPrompt] = {'contacted119': 'urgent_connected', 'cannot_call': 'urgent_support',
                                           'nearby_help': 'urgent_nearby', 'end': 'urgent_goodbye',
                                           'revoke': 'urgent_revoked', 'revoke_location': 'urgent_location_revoked',
                                           'continue': 'urgent_help'}
    kind = kinds[intent]
    action: Literal['continue', 'end', 'revoke', 'revoke_location'] = intent if intent in ('end', 'revoke', 'revoke_location') else 'continue'
    return UrgentReply(action=action, prompt_kind=kind, prompt=PROMPTS[kind])


def _offline_intent(text: str) -> UrgentIntent:
    """Honor only unambiguous controls when caller text cannot leave the server."""
    compact = ''.join(char for char in text if char.isalnum())
    if compact in {'동의철회', '동의취소', '개인정보동의철회', '개인정보동의취소',
                   '동의를철회합니다', '동의를철회해요', '동의철회할게요', '동의철회해요',
                   '동의취소해요', '개인정보동의철회해요'}:
        return 'revoke'
    if compact in {'위치동의철회', '위치동의취소', '위치사용철회', '위치사용취소'}:
        return 'revoke_location'
    if compact in {'끊을게요', '이제끊을게요', '전화를끊을게요', '상담종료할게요',
                   '그만할게요', '통화를마칠게요', '전화를끊겠습니다', '상담을마칠게요'}:
        return 'end'
    return 'continue'


def _require_active(call: Row | None, *, revoked: bool = False) -> Row:
    if call is None:
        raise HTTPException(404, 'Call not found')
    if call.get('ended_at'):
        raise HTTPException(409, 'Call has ended')
    expected_state = 'declined' if revoked else 'accepted'
    if call['state'] != expected_state:
        raise HTTPException(403, 'Consent is required')
    if not call['urgent']:
        raise HTTPException(409, 'Urgent assistance is not active')
    return call


def urgent_followup_router(database: Database, tts: SpeechOutput, provider: LowProvider,
                           queue_notice: Callable[[str], Awaitable[None]]) -> APIRouter:
    router = APIRouter()
    calls = CallStore(database)
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE IF NOT EXISTS urgent_followups (
            call_id TEXT NOT NULL REFERENCES calls(id), request_id TEXT NOT NULL,
            input_text TEXT NOT NULL, action TEXT NOT NULL, prompt_kind TEXT NOT NULL,
            user_location_generation INTEGER,
            user_location_revision INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(call_id,request_id))''')
        columns = {row[1] for row in con.execute('PRAGMA table_info(urgent_followups)')}
        if 'user_location_generation' not in columns:
            con.execute('ALTER TABLE urgent_followups ADD COLUMN user_location_generation INTEGER')
        if 'user_location_revision' not in columns:
            con.execute('ALTER TABLE urgent_followups ADD COLUMN user_location_revision INTEGER')

    def existing(call_id: str, body: UrgentFollowupInput) -> UrgentReply | None:
        with closing(database.connect()) as con:
            row = con.execute('SELECT * FROM urgent_followups WHERE call_id=? AND request_id=?',
                              (call_id, body.request_id)).fetchone()
        if row is None:
            return None
        if row['input_text'] != body.text:
            raise HTTPException(409, 'Request identifier already used')
        if ((body.source_location_generation is not None and row['user_location_generation'] != body.source_location_generation)
                or (body.source_location_revision is not None and row['user_location_revision'] != body.source_location_revision)):
            raise HTTPException(409, 'Request identifier already used in another location epoch')
        with closing(database.connect()) as con:
            if not user_generation_visible(con, call_id, row['user_location_generation']):
                return None
        return UrgentReply(action=row['action'], prompt_kind=row['prompt_kind'], prompt=PROMPTS[row['prompt_kind']])

    def history(call_id: str, source_generation: int | None, source_revision: int | None) -> tuple[int, int, list[UrgentHistory]]:
        with closing(database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            if not validate_source_generation(con, call_id, source_generation, source_revision):
                raise HTTPException(409, 'Location consent changed')
            generation = capture_location_generation(con, call_id)
            revision = capture_location_revision(con, call_id)
            rows = con.execute('''SELECT input_text,prompt_kind,user_location_generation FROM urgent_followups
                WHERE call_id=? ORDER BY rowid DESC''', (call_id,)).fetchall()
            visible = [row for row in rows if user_generation_visible(con, call_id, row['user_location_generation'])][:3]
        return generation, revision, [UrgentHistory(caller=row['input_text'], assistant=PROMPTS[row['prompt_kind']]) for row in reversed(visible)]

    def save(call_id: str, body: UrgentFollowupInput, result: UrgentReply, generation: int, revision: int) -> bool:
        with closing(database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            current = _require_active(dict(row) if row is not None else None)
            if not validate_source_generation(con, call_id, generation, revision):
                raise HTTPException(409, 'Location consent changed')
            previous = con.execute('SELECT input_text,user_location_generation,user_location_revision FROM urgent_followups WHERE call_id=? AND request_id=?',
                                   (call_id, body.request_id)).fetchone()
            if previous is not None:
                if (previous['input_text'] != body.text or previous['user_location_generation'] != generation
                        or previous['user_location_revision'] != revision):
                    raise HTTPException(409, 'Request identifier already used')
                return False
            con.execute('''INSERT INTO urgent_followups(call_id,request_id,input_text,action,prompt_kind,user_location_generation,user_location_revision)
                VALUES (?,?,?,?,?,?,?)''', (call_id, body.request_id, body.text, result.action, result.prompt_kind, generation, revision))
            if current['consent_version'] in ('municipality-v2', 'municipality-v3'):
                digest = urgent_detail_hash(body.text, generation, revision)
                con.execute('''INSERT OR IGNORE INTO urgent_detail_events(call_id,text_hash,text,user_location_generation,user_location_revision)
                    VALUES (?,?,?,?,?)''', (call_id, digest, body.text, generation, revision))
            return True

    def can_send(call_id: str, generation: int, revision: int) -> bool:
        with closing(database.connect()) as con:
            return (validate_source_generation(con, call_id, generation, revision)
                    and user_generation_visible(con, call_id, generation))

    def validate_source(call_id: str, source_generation: int | None, source_revision: int | None) -> bool:
        with closing(database.connect()) as con:
            return validate_source_generation(con, call_id, source_generation, source_revision)

    @router.post('/calls/{call_id}/urgent-followup', response_model=UrgentReply)
    async def followup(call_id: str, body: UrgentFollowupInput, background: BackgroundTasks) -> UrgentReply:
        _require_active(await anyio.to_thread.run_sync(calls.get, call_id))
        if not await anyio.to_thread.run_sync(validate_source, call_id, body.source_location_generation, body.source_location_revision):
            raise HTTPException(409, 'Location consent changed')
        previous = await anyio.to_thread.run_sync(existing, call_id, body)
        if previous is not None:
            return previous
        generation, revision, recent = await anyio.to_thread.run_sync(history, call_id, body.source_location_generation, body.source_location_revision)
        if await anyio.to_thread.run_sync(can_send, call_id, generation, revision):
            result = _reply(await classify_urgent(provider, body.text, recent))
        else:
            result = _reply(_offline_intent(body.text))
        _require_active(await anyio.to_thread.run_sync(calls.get, call_id))
        if not await anyio.to_thread.run_sync(validate_source, call_id, generation, revision):
            raise HTTPException(409, 'Location consent changed')
        if result.action == 'revoke':
            if not await anyio.to_thread.run_sync(revoke_call, database, call_id):
                raise HTTPException(403, 'Consent is required')
            return result
        if result.action == 'revoke_location':
            changed = await anyio.to_thread.run_sync(partial(RecommendationOutbox(database).set_location_consent_with_epoch,
                call_id, False, source_location_generation=generation, source_location_revision=revision))
            if changed is None:
                raise HTTPException(409, 'Consent or call status changed')
            return result.model_copy(update={'location_generation': 0, 'location_revision': changed[3]})
        if result.action == 'end':
            await anyio.to_thread.run_sync(end_call, database, call_id, 'caller')
            return result
        created = await anyio.to_thread.run_sync(save, call_id, body, result, generation, revision)
        saved = await anyio.to_thread.run_sync(existing, call_id, body)
        if created:
            background.add_task(queue_notice, call_id)
        return saved or result

    @router.get('/calls/{call_id}/urgent-followup/audio/{kind}')
    async def audio(call_id: str, kind: UrgentPrompt) -> Response:
        _require_active(await anyio.to_thread.run_sync(calls.get, call_id), revoked=kind == 'urgent_revoked')
        text = PROMPTS[kind]
        try:
            speed = await anyio.to_thread.run_sync(call_speech_speed, database, call_id)
            result = await deliver_at_speed(tts, text, 'prompt', speed)
        except (ServiceError, httpx2.HTTPError, TimeoutError, OSError) as exc:
            raise HTTPException(503, 'Speech synthesis unavailable') from exc
        _require_active(await anyio.to_thread.run_sync(calls.get, call_id), revoked=kind == 'urgent_revoked')
        if result.status != 'synthesized' or not result.audio or result.mime_type is None:
            raise HTTPException(503, 'Speech synthesis unavailable')
        return Response(content=result.audio, media_type=result.mime_type, headers={
            'Cache-Control': 'no-store', 'X-Spoken-Text-Base64': base64.b64encode(text.encode('utf-8')).decode('ascii')})

    return router
