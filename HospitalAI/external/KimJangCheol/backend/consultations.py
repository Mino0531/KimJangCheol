"""Authenticated intake for generated telephone consultation results.

These records remain isolated from the legacy unauthenticated demo endpoints.
"""

from datetime import datetime
from hmac import compare_digest
import json
from os import getenv
import re
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field as InputField, ValidationError, model_validator
from sqlalchemy import Column, JSON, UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.location_tombstones import redact_location_event

INTERRUPTION_NOTICES = frozenset({
    '응답이 중단됐습니다.',
    '연결 오류로 응답이 중단됐습니다.',
    '통화가 종료되어 응답이 중단됐습니다.',
})


class ConsultationRecord(SQLModel, table=True):
    __tablename__ = 'consultation_results'
    __table_args__ = (UniqueConstraint('source', 'event_id'),)
    id: str = Field(primary_key=True)
    source: str
    event_id: str
    occurred_at: str = Field(index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class SourceMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str = InputField(min_length=1, max_length=256)
    type: str = InputField(min_length=1, max_length=128)
    source: str = InputField(min_length=1, max_length=10000)


class _DisplayAnswer(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    display: str | None = InputField(default=None, validation_alias=AliasChoices(
        'display_text', 'displayText', 'display', '표시', '표시할 내용', '화면 표시'))
    speech: str | None = InputField(default=None, validation_alias=AliasChoices(
        'tts_text', 'ttsText', 'speech_text', 'speech', 'tts', 'TTS', '음성', 'TTS출력할 내용'))


def _looks_structured(text: str) -> bool:
    return (text.startswith(('{', '```')) or bool(re.match(r'^\[\s*[{\"]', text))
            or bool(re.search(r'\{\s*"[^"\\]{1,80}"\s*:', text)))


def display_answer(value: str) -> str:
    """Render only an answer field, while leaving the received event unchanged."""
    fallback = '답변 형식을 확인할 수 없습니다. 원본을 검토해 주세요.'
    raw = value.strip()
    if raw.startswith('"'):
        nested = raw
        for _ in range(4):
            try:
                decoded = json.loads(nested)
            except json.JSONDecodeError:
                if nested != raw or raw.startswith(('"{', '"\\"')):
                    return fallback
                break
            if not isinstance(decoded, str):
                break
            decoded = decoded.strip()
            if _looks_structured(decoded):
                raw = decoded
                break
            if not decoded.startswith('"'):
                break
            nested = decoded
        else:
            return fallback
    if raw.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', raw, re.IGNORECASE)
        if match is None:
            return fallback
        raw = match.group(1).strip()
        if not raw.startswith('{'):
            return fallback
    embedded = re.search(r'\{\s*"[^"\\]{1,80}"\s*:', raw)
    if embedded is not None and not raw.startswith('{'):
        raw = raw[embedded.start():]
    if raw.startswith('{') or re.match(r'^\[\s*[{\"]', raw):
        try:
            parsed = _DisplayAnswer.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            return fallback
        display = (parsed.display or parsed.speech or '').strip()
        return display if display and not _looks_structured(display) else fallback
    return raw or fallback


class ConsultationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: str = InputField(min_length=1, max_length=100)
    event_id: str = InputField(min_length=1, max_length=128)
    call_id: str = InputField(min_length=1, max_length=128)
    occurred_at: datetime
    phone: str = InputField(min_length=1, max_length=32)
    user_text: str = InputField(min_length=1, max_length=10000)
    assistant_text: str = InputField(min_length=1, max_length=10000)
    answer_status: Literal['completed', 'degraded', 'emergency', 'interrupted']
    sources: list[SourceMetadata] = InputField(max_length=100)

    @model_validator(mode='after')
    def timezone_required(self) -> 'ConsultationInput':
        if self.occurred_at.utcoffset() is None:
            raise ValueError('occurred_at must include a timezone')
        if self.answer_status == 'interrupted' and (
                self.assistant_text not in INTERRUPTION_NOTICES or self.sources):
            raise ValueError('interrupted turns require the interruption notice and no sources')
        return self


def require_dashboard_token(authorization: Annotated[str | None, Header()] = None) -> None:
    token = getenv('HOSPITAL_DASHBOARD_TOKEN', '')
    if not token or not authorization or not compare_digest(authorization, f'Bearer {token}'):
        raise HTTPException(401, 'Authentication required')


def register_consultation_routes(app: FastAPI, engine: Engine) -> None:
    @app.post('/api/team/consultation-results', dependencies=[Depends(require_dashboard_token)])
    def receive_result(body: ConsultationInput) -> JSONResponse:
        payload = body.model_dump(mode='json')
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            previous = session.exec(select(ConsultationRecord).where(
                ConsultationRecord.source == body.source, ConsultationRecord.event_id == body.event_id)).first()
            if previous:
                if previous.data != payload:
                    raise HTTPException(409, 'Event ID already used with different content')
                return JSONResponse({'applied': False, 'duplicate': True, 'event_id': body.event_id,
                                     'call_id': body.call_id}, status_code=200)
            session.add(ConsultationRecord(id=str(uuid4()), source=body.source, event_id=body.event_id,
                                           occurred_at=body.occurred_at.isoformat(), data=payload))
            session.commit()
        return JSONResponse({'applied': True, 'duplicate': False, 'event_id': body.event_id,
                             'call_id': body.call_id}, status_code=201)

    @app.get('/api/team/consultation-results', dependencies=[Depends(require_dashboard_token)])
    def list_results(response: Response, limit: int = Query(100, ge=1, le=200),
                     offset: int = Query(0, ge=0)) -> list[dict]:
        # Deferred import avoids the intake module's SourceMetadata dependency.
        from backend.municipal_intake import MunicipalEvent

        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            rows = session.exec(select(ConsultationRecord).order_by(
                ConsultationRecord.occurred_at.desc(), ConsultationRecord.id).offset(offset).limit(limit)).all()
            result = []
            for row in rows:
                event = session.exec(select(MunicipalEvent).where(
                    MunicipalEvent.source == row.source,
                    MunicipalEvent.event_id == row.event_id)).first()
                provenance = dict(row.data)
                matches = event is not None and event.call_id == row.data.get('call_id') and all(
                    event.data.get(key) == row.data.get(key)
                    for key in ('user_text', 'assistant_text', 'answer_status'))
                if matches and event is not None:
                    provenance['location_generations'] = event.data.get('location_generations')
                    provenance['recommendation_event_ids'] = event.data.get('recommendation_event_ids')
                    provenance['user_location_generation'] = event.data.get('user_location_generation')
                    provenance['sources'] = event.data.get('sources', [])
                else:
                    # Direct or legacy records lack authoritative provenance.
                    provenance.pop('location_generations', None)
                    provenance.pop('recommendation_event_ids', None)
                    provenance.pop('user_location_generation', None)
                visible = redact_location_event(session, provenance)
                if visible['assistant_text']:
                    visible['assistant_text'] = display_answer(visible['assistant_text'])
                result.append(visible)
            return result
