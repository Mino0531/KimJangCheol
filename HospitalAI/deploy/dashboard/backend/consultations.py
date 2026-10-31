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
                self.assistant_text != '응답이 중단됐습니다.' or self.sources):
            raise ValueError('interrupted turns require the interruption notice and no sources')
        return self


def require_dashboard_token(authorization: Annotated[str | None, Header()] = None) -> None:
    token = getenv('HOSPITAL_DASHBOARD_TOKEN', '')
    if not token or not token.isascii() or not authorization or not authorization.isascii() or not compare_digest(authorization, f'Bearer {token}'):
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
        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            rows = session.exec(select(ConsultationRecord).order_by(
                ConsultationRecord.occurred_at.desc(), ConsultationRecord.id).offset(offset).limit(limit)).all()
            return [{**row.data, 'assistant_text': display_answer(row.data['assistant_text'])} for row in rows]
