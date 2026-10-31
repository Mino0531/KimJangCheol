"""Authenticated municipal inbox, independent from synthetic demo cases."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field as InputField, model_validator
from sqlalchemy import Column, JSON, UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import SourceMetadata, display_answer
from backend.municipal_auth import require_municipal_intake, require_municipal_staff


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


class MunicipalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['hospital-ai']
    event_id: str = InputField(min_length=1, max_length=128)
    call_id: str = InputField(min_length=1, max_length=128)
    occurred_at: datetime
    event_type: Literal['call_started', 'turn_completed', 'turn_interrupted', 'playback_completed', 'urgent']
    phone: str = InputField(min_length=1, max_length=32)
    user_text: str = InputField(default='', max_length=10000)
    assistant_text: str = InputField(default='', max_length=10000)
    answer_status: Literal['completed', 'degraded', 'emergency', 'interrupted'] | None = None
    sources: list[SourceMetadata] = InputField(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def valid_event(self) -> 'MunicipalInput':
        if self.occurred_at.utcoffset() is None:
            raise ValueError('occurred_at must include a timezone')
        if self.event_type == 'urgent' and self.answer_status != 'emergency':
            raise ValueError('urgent events require emergency status')
        if self.event_type == 'turn_completed' and (not self.user_text.strip() or not self.assistant_text.strip() or self.answer_status is None):
            raise ValueError('completed turns require text and status')
        if self.event_type == 'turn_interrupted' and (
                not self.user_text.strip() or self.assistant_text != '응답이 중단됐습니다.'
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
        return self


class VersionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    expected_version: int = InputField(ge=1)


def initialize_municipal_schema(engine: Engine) -> None:
    """Upgrade early local inbox databases without changing existing cases."""
    with engine.begin() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        columns = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(municipal_cases)')}
        if columns and 'acknowledged_at' not in columns:
            connection.exec_driver_sql('ALTER TABLE municipal_cases ADD COLUMN acknowledged_at VARCHAR')


def _case(session: Session, row: MunicipalCase) -> dict:
    events = session.exec(select(MunicipalEvent).where(MunicipalEvent.call_id == row.call_id)
                          .order_by(MunicipalEvent.occurred_at, MunicipalEvent.id)).all()
    items = []
    for event in events:
        item = dict(event.data)
        if item['assistant_text']:
            item['assistant_text'] = display_answer(item['assistant_text'])
        items.append(item)
    return {**row.model_dump(), 'events': items}


def register_municipal_intake_routes(app: FastAPI, engine: Engine) -> None:
    @app.post('/api/municipality/intake', dependencies=[Depends(require_municipal_intake)])
    def intake(body: MunicipalInput) -> JSONResponse:
        payload = body.model_dump(mode='json')
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            prior = session.exec(select(MunicipalEvent).where(MunicipalEvent.source == body.source,
                                  MunicipalEvent.event_id == body.event_id)).first()
            if prior:
                if prior.data != payload:
                    raise HTTPException(409, 'Event ID already used with different content')
                return JSONResponse({'applied': False, 'duplicate': True, 'event_id': body.event_id,
                                     'call_id': body.call_id}, status_code=200)
            row = session.get(MunicipalCase, body.call_id)
            at = datetime.now(timezone.utc).isoformat()
            if row is None:
                row = MunicipalCase(call_id=body.call_id, source=body.source, phone=body.phone,
                                    created_at=at, updated_at=at, urgent=body.event_type == 'urgent')
            else:
                if row.source != body.source or row.phone != body.phone:
                    raise HTTPException(409, 'Call identity differs from original')
                row.version += 1
                row.updated_at = at
                row.unread = True
                row.status = 'received'
                row.acknowledged_at = None
                row.urgent = row.urgent or body.event_type == 'urgent' or body.answer_status == 'emergency'
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
