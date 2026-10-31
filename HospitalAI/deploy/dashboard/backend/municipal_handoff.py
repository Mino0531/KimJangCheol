"""Atomically hand reviewed municipal consultations to the local hospital inbox."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field as InputField, ValidationError
from sqlalchemy import UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import ConsultationInput, ConsultationRecord, display_answer, require_dashboard_token
from backend.municipal_auth import require_municipal_staff
from backend.municipal_intake import MunicipalCase, MunicipalEvent


class MunicipalHandoff(SQLModel, table=True):
    __tablename__ = 'municipal_handoffs'
    __table_args__ = (UniqueConstraint('call_id', 'reviewed_version'),)

    id: str = Field(primary_key=True)
    call_id: str = Field(index=True)
    reviewed_version: int
    handed_at: str
    event_count: int


class MunicipalHandoffEvent(SQLModel, table=True):
    __tablename__ = 'municipal_handoff_events'
    __table_args__ = (UniqueConstraint('handoff_id', 'consultation_id'),)

    id: str = Field(primary_key=True)
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', index=True)
    consultation_id: str = Field(foreign_key='consultation_results.id')
    source: str
    event_id: str


class MunicipalHandoffPlayback(SQLModel, table=True):
    """Playback facts included in an explicitly reviewed handoff."""
    __tablename__ = 'municipal_handoff_playback'
    __table_args__ = (UniqueConstraint('handoff_id', 'source', 'event_id'),)

    id: str = Field(primary_key=True)
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', index=True)
    source: str
    event_id: str


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    expected_version: int = InputField(ge=1)


def consultation_payload(event: MunicipalEvent) -> dict | None:
    """Preserve source identities while excluding starts and location recommendations."""
    if event.event_type not in ('turn_completed', 'turn_interrupted', 'urgent'):
        return None
    raw = {name: event.data.get(name) for name in ConsultationInput.model_fields}
    if not raw.get('assistant_text') or not raw.get('answer_status'):
        raise HTTPException(409, '인계할 상담 내용이 완성되지 않았습니다.')
    if event.event_type == 'urgent' and not raw.get('user_text'):
        raw['user_text'] = '긴급 발화 내용 미기록'
    raw.pop('event_type', None)
    try:
        return ConsultationInput.model_validate(raw).model_dump(mode='json')
    except ValidationError as exc:
        raise HTTPException(409, '인계할 상담 자료의 형식을 확인해 주세요.') from exc


def register_municipal_handoff_routes(app: FastAPI, engine: Engine) -> None:
    @app.post('/api/municipality/inbox/{call_id}/handoff', dependencies=[Depends(require_municipal_staff)])
    def handoff(call_id: str, body: HandoffRequest) -> dict:
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            case = session.get(MunicipalCase, call_id)
            if case is None:
                raise HTTPException(404, '접수된 통화가 없습니다.')
            prior = session.exec(select(MunicipalHandoff).where(
                MunicipalHandoff.call_id == call_id,
                MunicipalHandoff.reviewed_version == body.expected_version)).first()
            if prior is not None:
                return {'duplicate': True, 'handoff_id': prior.id, 'event_count': prior.event_count,
                        'status': case.status, 'version': case.version}
            if case.version != body.expected_version:
                raise HTTPException(409, '통화 내용이 변경됐습니다. 다시 확인해 주세요.')
            if case.unread or case.status != 'acknowledged':
                raise HTTPException(409, '담당자가 통화를 확인한 뒤 인계해 주세요.')
            events = session.exec(select(MunicipalEvent).where(MunicipalEvent.call_id == call_id)).all()
            payloads = [payload for event in events if (payload := consultation_payload(event)) is not None]
            if not payloads:
                raise HTTPException(409, '인계할 상담 또는 긴급 안내가 없습니다.')
            receipt = MunicipalHandoff(id=str(uuid4()), call_id=call_id,
                reviewed_version=body.expected_version, handed_at=datetime.now(timezone.utc).isoformat(),
                event_count=len(payloads))
            session.add(receipt)
            session.flush()
            for payload in payloads:
                previous = session.exec(select(ConsultationRecord).where(
                    ConsultationRecord.source == payload['source'],
                    ConsultationRecord.event_id == payload['event_id'])).first()
                if previous:
                    if previous.data != payload:
                        raise HTTPException(409, '동일한 인계 식별자에 다른 내용이 있습니다.')
                    record = previous
                else:
                    record = ConsultationRecord(id=str(uuid4()), source=payload['source'],
                        event_id=payload['event_id'], occurred_at=payload['occurred_at'], data=payload)
                    session.add(record)
                    session.flush()
                session.add(MunicipalHandoffEvent(id=str(uuid4()), handoff_id=receipt.id,
                    consultation_id=record.id, source=payload['source'], event_id=payload['event_id']))
            # Snapshot only facts reviewed in this handoff, never later inbox updates.
            for event in events:
                if event.event_type == 'playback_completed':
                    session.add(MunicipalHandoffPlayback(id=str(uuid4()), handoff_id=receipt.id,
                        source=event.source, event_id=event.event_id))
            case.status = 'handed_over'
            case.version += 1
            case.updated_at = receipt.handed_at
            session.add(case)
            session.add(receipt)
            session.commit()
            return {'duplicate': False, 'handoff_id': receipt.id, 'event_count': receipt.event_count,
                    'status': case.status, 'version': case.version}

    @app.get('/api/team/municipality-handoffs', dependencies=[Depends(require_dashboard_token)])
    def hospital_handoffs(response: Response) -> dict:
        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            receipts = session.exec(select(MunicipalHandoff).order_by(MunicipalHandoff.handed_at.desc())).all()
            handoffs = []
            seen_events: set[tuple[str, str]] = set()
            for receipt in receipts:
                links = session.exec(select(MunicipalHandoffEvent).where(
                    MunicipalHandoffEvent.handoff_id == receipt.id)).all()
                entries = []
                for link in links:
                    identity = (link.source, link.event_id)
                    if identity in seen_events:
                        continue
                    record = session.get(ConsultationRecord, link.consultation_id)
                    if record is not None and record.source == link.source and record.event_id == link.event_id:
                        entries.append({**record.data, 'assistant_text': display_answer(record.data['assistant_text'])})
                        seen_events.add(identity)
                if entries:
                    playback = session.exec(select(MunicipalHandoffPlayback).where(
                        MunicipalHandoffPlayback.handoff_id == receipt.id)).all()
                    handoffs.append({'handoff_id': receipt.id, 'handed_at': receipt.handed_at,
                                     'call_id': receipt.call_id, 'events': entries,
                                     'playback_completed_event_ids': [event.event_id for event in playback]})
            return {'handoffs': handoffs}
