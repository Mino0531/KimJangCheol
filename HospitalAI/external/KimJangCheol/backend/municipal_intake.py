"""Authenticated municipal inbox, independent from synthetic demo cases."""
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field as InputField, model_validator
from sqlalchemy import Column, JSON, UniqueConstraint, case, literal_column
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import INTERRUPTION_NOTICES, SourceMetadata, display_answer
from backend.location_tombstones import initialize_location_tombstones, record_location_revocation, redact_location_event
from backend.municipal_auth import require_municipal_intake, require_municipal_staff
from backend.recommendations import HospitalSummary


class MunicipalCase(SQLModel, table=True):
    __tablename__ = 'municipal_cases'
    call_id: str = Field(primary_key=True)
    source: str
    phone: str
    created_at: str
    updated_at: str = Field(index=True)
    urgent: bool = False
    unread: bool = True
    version: int = 1
    status: str = 'received'
    acknowledged_at: str | None = None


class MunicipalEvent(SQLModel, table=True):
    __tablename__ = 'municipal_events'
    __table_args__ = (UniqueConstraint('source', 'event_id'),)
    id: str = Field(primary_key=True)
    source: str
    event_id: str
    call_id: str = Field(index=True)
    occurred_at: str
    event_type: str
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class RecommendationSummary(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    specialty: str | None = InputField(default=None, max_length=100)
    hospitals: list[HospitalSummary] = InputField(max_length=50)
    warning: str | None = InputField(default=None, max_length=4000)


class MunicipalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['hospital-ai']
    event_id: str = InputField(min_length=1, max_length=128)
    call_id: str = InputField(min_length=1, max_length=128)
    occurred_at: datetime
    event_type: Literal['call_started', 'caller_utterance', 'privacy_redacted', 'turn_completed', 'turn_interrupted', 'playback_completed', 'call_ended', 'urgent', 'recommendation', 'location_revoked']
    phone: str = InputField(min_length=1, max_length=32)
    user_text: str = InputField(default='', max_length=10000)
    assistant_text: str = InputField(default='', max_length=10000)
    answer_status: Literal['completed', 'degraded', 'emergency', 'interrupted'] | None = None
    sources: list[SourceMetadata] = InputField(default_factory=list, max_length=100)
    utterance_id: str | None = InputField(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._-]+$')
    user_location_generation: int | None = InputField(default=None, ge=0, strict=True)
    user_location_revision: int | None = InputField(default=None, ge=0, strict=True)
    end_reason: Literal['caller', 'hangup', 'connection_lost', 'safety_stop', 'consent_declined'] | None = None
    location_generation: int | None = InputField(default=None, ge=1, strict=True)
    location_generations: list[Annotated[int, InputField(ge=1, strict=True)]] | None = InputField(default=None, max_length=100)
    recommendation_event_ids: list[Annotated[str, InputField(min_length=1, max_length=128)]] | None = InputField(default=None, max_length=100)
    recommendation: RecommendationSummary | None = None

    @model_validator(mode='after')
    def valid_event(self) -> 'MunicipalInput':
        if self.occurred_at.utcoffset() is None:
            raise ValueError('occurred_at must include a timezone')
        if self.event_type == 'urgent' and self.answer_status != 'emergency':
            raise ValueError('urgent events require emergency status')
        if self.event_type == 'turn_completed' and (not self.user_text.strip() or not self.assistant_text.strip() or self.answer_status is None):
            raise ValueError('completed turns require text and status')
        if self.event_type == 'turn_interrupted' and (
                not self.user_text.strip() or self.assistant_text not in INTERRUPTION_NOTICES
                or self.answer_status != 'interrupted' or self.sources):
            raise ValueError('interrupted turns require caller text and the interruption notice')
        if self.event_type != 'turn_interrupted' and self.answer_status == 'interrupted':
            raise ValueError('interrupted status requires an interrupted turn')
        if self.event_type == 'playback_completed' and (
                not self.event_id.startswith('playback:') or len(self.event_id) <= len('playback:')
                or self.user_text or self.assistant_text or self.answer_status is not None or self.sources):
            raise ValueError('playback receipt must not contain consultation text')
        if self.event_type == 'call_started' and (self.user_text or self.assistant_text or self.answer_status):
            raise ValueError('call start cannot contain consultation text')
        if self.event_type == 'caller_utterance':
            if (self.utterance_id is None or not self.user_text.strip() or self.assistant_text
                    or self.answer_status is not None or self.sources):
                raise ValueError('Caller utterance requires only caller text and an utterance ID')
            digest = sha256(f'{self.call_id}:{self.utterance_id}'.encode('utf-8')).hexdigest()
            if self.event_id != f'utterance:{digest}':
                raise ValueError('Caller utterance event ID must match its call and utterance ID')
        elif self.utterance_id is not None and self.event_type not in ('turn_completed', 'turn_interrupted'):
            raise ValueError('Utterance ID requires a caller utterance or consultation turn')
        if self.event_type == 'privacy_redacted':
            if (re.fullmatch(r'redacted:[0-9a-f]{64}', self.event_id) is None
                    or self.user_text != '위치 정보가 포함될 수 있어 원문 확인 제한'
                    or self.assistant_text or self.answer_status is not None or self.sources
                    or self.utterance_id is not None or self.location_generations
                    or self.recommendation_event_ids):
                raise ValueError('Privacy-redacted events cannot contain consultation content')
        if self.event_type == 'call_ended':
            if (self.event_id != f'end:{self.call_id}' or self.end_reason is None or self.user_text
                    or self.assistant_text or self.answer_status is not None or self.sources):
                raise ValueError('call end must only contain a valid reason')
        elif self.end_reason is not None:
            raise ValueError('end reason requires a call end event')
        if self.event_type in ('recommendation', 'location_revoked'):
            if self.location_generation is None or self.user_text or self.assistant_text or self.answer_status or self.sources:
                raise ValueError('Location events require a generation and no consultation text')
            if (self.event_type == 'recommendation') != (self.recommendation is not None):
                raise ValueError('Recommendation payload required only for recommendation events')
        elif self.location_generation is not None or self.recommendation is not None:
            raise ValueError('Unexpected location event payload')
        return self


class VersionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    expected_version: int = InputField(ge=1)


def initialize_municipal_schema(engine: Engine) -> None:
    """Upgrade early local inbox databases without changing existing cases."""
    initialize_location_tombstones(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        columns = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(municipal_cases)')}
        if columns and 'acknowledged_at' not in columns:
            connection.exec_driver_sql('ALTER TABLE municipal_cases ADD COLUMN acknowledged_at VARCHAR')


def _case(session: Session, row: MunicipalCase) -> dict:
    lifecycle_order = case(
        (MunicipalEvent.event_type == 'call_started', 0),
        (MunicipalEvent.event_type.in_(['caller_utterance', 'privacy_redacted']), 1),
        (MunicipalEvent.event_type.in_(['turn_completed', 'turn_interrupted', 'urgent']), 2),
        (MunicipalEvent.event_type == 'recommendation', 3),
        (MunicipalEvent.event_type.in_(['playback_completed', 'location_revoked']), 4),
        (MunicipalEvent.event_type == 'call_ended', 5),
        else_=6,
    )
    events = session.exec(select(MunicipalEvent).where(MunicipalEvent.call_id == row.call_id)
                          .order_by(MunicipalEvent.occurred_at, lifecycle_order,
                                    literal_column('municipal_events.rowid'))).all()
    items = []
    for event in events:
        item = redact_location_event(session, dict(event.data))
        if item['assistant_text']:
            item['assistant_text'] = display_answer(item['assistant_text'])
        items.append(item)
    from backend.municipal_workflow import hospital_handed_over, hospital_workflow, workflow_history
    return {**row.model_dump(), 'events': items, 'workflow_history': workflow_history(session, row.call_id),
            'hospital_workflow': hospital_workflow(session, row.call_id),
            'hospital_handed_over': hospital_handed_over(session, row.call_id)}


def register_municipal_intake_routes(app: FastAPI, engine: Engine) -> None:
    @app.get('/api/municipality/intake/capabilities', dependencies=[Depends(require_municipal_intake)])
    def intake_capabilities() -> dict[str, bool]:
        return {'caller_utterance': True, 'location_tombstones': True, 'privacy_redacted': True}

    @app.post('/api/municipality/intake', dependencies=[Depends(require_municipal_intake)])
    def intake(body: MunicipalInput) -> JSONResponse:
        payload = body.model_dump(mode='json')
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            prior = session.exec(select(MunicipalEvent).where(MunicipalEvent.source == body.source,
                                  MunicipalEvent.event_id == body.event_id)).first()
            if prior:
                if MunicipalInput.model_validate(prior.data).model_dump(mode='json') != payload:
                    raise HTTPException(409, 'Event ID already used with different content')
                return JSONResponse({'applied': False, 'duplicate': True, 'event_id': body.event_id,
                                     'call_id': body.call_id}, status_code=200)
            answered = False
            if body.utterance_id is not None:
                related = session.exec(select(MunicipalEvent).where(MunicipalEvent.source == body.source,
                                       MunicipalEvent.call_id == body.call_id)).all()
                related = [event for event in related if event.data.get('utterance_id') == body.utterance_id]
                if any(event.data.get('user_text') != body.user_text for event in related):
                    raise HTTPException(409, 'Utterance ID already used with different caller text')
                answered = body.event_type == 'caller_utterance' and any(
                    event.event_type in ('turn_completed', 'turn_interrupted') for event in related)
            row = session.get(MunicipalCase, body.call_id)
            at = datetime.now(timezone.utc).isoformat()
            if row is None:
                row = MunicipalCase(call_id=body.call_id, source=body.source, phone=body.phone,
                                    created_at=at, updated_at=at, urgent=body.event_type == 'urgent')
            else:
                if row.source != body.source or row.phone != body.phone:
                    raise HTTPException(409, 'Call identity differs from original')
                if body.event_type not in ('playback_completed', 'call_ended') and not answered:
                    row.version += 1
                    row.updated_at = at
                    row.unread = True
                    row.status = 'received'
                    row.acknowledged_at = None
                    row.urgent = row.urgent or body.event_type == 'urgent' or body.answer_status == 'emergency'
            if body.event_type == 'location_revoked' and body.location_generation is not None:
                record_location_revocation(session, body.source, body.call_id, body.location_generation)
            session.add(row)
            session.add(MunicipalEvent(id=str(uuid4()), source=body.source, event_id=body.event_id,
                                       call_id=body.call_id, occurred_at=body.occurred_at.isoformat(),
                                       event_type=body.event_type, data=payload))
            session.commit()
        return JSONResponse({'applied': True, 'duplicate': False, 'event_id': body.event_id,
                             'call_id': body.call_id}, status_code=201)

    @app.get('/api/municipality/inbox', dependencies=[Depends(require_municipal_staff)])
    def inbox(response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            rows = session.exec(select(MunicipalCase).order_by(MunicipalCase.updated_at.desc())).all()
            return {'cases': [_case(session, row) for row in rows]}

    @app.get('/api/municipality/inbox/{call_id}', dependencies=[Depends(require_municipal_staff)])
    def detail(call_id: str, response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            row = session.get(MunicipalCase, call_id)
            if row is None:
                raise HTTPException(404, '상담이 없습니다.')
            return _case(session, row)

    @app.post('/api/municipality/inbox/{call_id}/ack', dependencies=[Depends(require_municipal_staff)])
    def acknowledge(call_id: str, body: VersionInput) -> dict:
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = session.get(MunicipalCase, call_id)
            if row is None:
                raise HTTPException(404, '상담이 없습니다.')
            if row.version != body.expected_version:
                raise HTTPException(409, '상담이 변경되었습니다. 다시 확인해 주세요.')
            if not row.unread:
                raise HTTPException(409, '이미 확인한 상담입니다.')
            row.unread = False
            row.status = 'acknowledged'
            row.version += 1
            row.updated_at = datetime.now(timezone.utc).isoformat()
            row.acknowledged_at = row.updated_at
            session.add(row)
            session.commit()
            session.refresh(row)
            return {'case': _case(session, row)}
