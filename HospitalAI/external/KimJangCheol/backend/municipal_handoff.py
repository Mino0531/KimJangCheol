"""Atomically hand reviewed municipal consultations to the local hospital inbox."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic import Field as InputField
from sqlalchemy import UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import (
    ConsultationInput,
    ConsultationRecord,
    display_answer,
    require_dashboard_token,
)
from backend.location_tombstones import (
    initialize_location_tombstones,
    redact_location_event,
)
from backend.municipal_auth import require_municipal_staff
from backend.municipal_intake import MunicipalCase, MunicipalEvent
from backend.municipal_workflow import (
    hospital_workflow,
    register_workflow_routes,
    reset_for_new_handoff,
)


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


class MunicipalHandoffEnd(SQLModel, table=True):
    """Call-end metadata captured when staff explicitly handed off the case."""
    __tablename__ = 'municipal_handoff_ends'
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', primary_key=True)
    end_reason: str
    occurred_at: str


class MunicipalHandoffRecommendation(SQLModel, table=True):
    """Reference only recommendations included in an explicit staff review."""
    __tablename__ = 'municipal_handoff_recommendations'
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', primary_key=True)
    municipal_event_id: str = Field(foreign_key='municipal_events.id', primary_key=True)


class MunicipalHandoffUtterance(SQLModel, table=True):
    """Reference caller-only utterances reviewed before an answer was available."""
    __tablename__ = 'municipal_handoff_utterances'
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', primary_key=True)
    municipal_event_id: str = Field(foreign_key='municipal_events.id', primary_key=True)


class MunicipalHandoffPrivacy(SQLModel, table=True):
    """Reference a reviewed privacy-only notice without inventing an answer status."""
    __tablename__ = 'municipal_handoff_privacy'
    handoff_id: str = Field(foreign_key='municipal_handoffs.id', primary_key=True)
    municipal_event_id: str = Field(foreign_key='municipal_events.id', primary_key=True)


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
    initialize_location_tombstones(engine)
    register_workflow_routes(app, engine)
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
            if case.unread or case.status not in ('acknowledged', 'in_progress'):
                raise HTTPException(409, '담당자가 통화를 확인한 뒤 인계해 주세요.')
            events = session.exec(select(MunicipalEvent).where(MunicipalEvent.call_id == call_id)).all()
            payloads = [(event, payload) for event in events if (payload := consultation_payload(event)) is not None]
            answered = {(event.source, event.data.get('utterance_id')) for event, _ in payloads
                        if event.data.get('utterance_id')}
            unanswered = [event for event in events if event.event_type == 'caller_utterance'
                          and event.data.get('utterance_id') and event.data.get('user_text')
                          and (event.source, event.data['utterance_id']) not in answered]
            privacy = [event for event in events if event.event_type == 'privacy_redacted']
            if not payloads and not unanswered and not privacy:
                raise HTTPException(409, '인계할 상담 또는 긴급 안내가 없습니다.')
            receipt = MunicipalHandoff(id=str(uuid4()), call_id=call_id,
                reviewed_version=body.expected_version, handed_at=datetime.now(UTC).isoformat(),
                event_count=len(payloads) + len(unanswered) + len(privacy))
            session.add(receipt)
            session.flush()
            for event, payload in payloads:
                redacted = redact_location_event(session, event.data)
                safe_payload = {**payload, 'user_text': redacted.get('user_text') or payload['user_text']}
                if redacted.get('assistant_text') != event.data.get('assistant_text'):
                    notice = ('응답이 중단됐습니다.' if payload['answer_status'] == 'interrupted'
                              else '위치 동의 철회로 안내가 제외되었습니다.')
                    safe_payload = {**safe_payload, 'assistant_text': notice, 'sources': []}
                previous = session.exec(select(ConsultationRecord).where(
                    ConsultationRecord.source == payload['source'],
                    ConsultationRecord.event_id == payload['event_id'])).first()
                if previous:
                    if previous.data != payload and previous.data != safe_payload:
                        raise HTTPException(409, '동일한 인계 식별자에 다른 내용이 있습니다.')
                    record = previous
                else:
                    record = ConsultationRecord(id=str(uuid4()), source=payload['source'],
                        event_id=payload['event_id'], occurred_at=payload['occurred_at'], data=safe_payload)
                    session.add(record)
                    session.flush()
                session.add(MunicipalHandoffEvent(id=str(uuid4()), handoff_id=receipt.id,
                    consultation_id=record.id, source=payload['source'], event_id=payload['event_id']))
            # Snapshot only facts reviewed in this handoff, never later inbox updates.
            for event in events:
                if event.event_type == 'playback_completed':
                    session.add(MunicipalHandoffPlayback(id=str(uuid4()), handoff_id=receipt.id,
                        source=event.source, event_id=event.event_id))
                if event.event_type == 'recommendation' and redact_location_event(session, event.data).get('recommendation'):
                    session.add(MunicipalHandoffRecommendation(handoff_id=receipt.id, municipal_event_id=event.id))
            for event in unanswered:
                session.add(MunicipalHandoffUtterance(handoff_id=receipt.id, municipal_event_id=event.id))
            for event in privacy:
                session.add(MunicipalHandoffPrivacy(handoff_id=receipt.id, municipal_event_id=event.id))
            ends = [event for event in events if event.event_type == 'call_ended' and event.data.get('end_reason')]
            if ends:
                end = max(ends, key=lambda event: event.occurred_at)
                session.add(MunicipalHandoffEnd(handoff_id=receipt.id,
                    end_reason=end.data['end_reason'], occurred_at=end.occurred_at))
            case.status = 'handed_over'
            case.version += 1
            case.updated_at = receipt.handed_at
            session.add(case)
            session.add(receipt)
            reset_for_new_handoff(session, call_id)
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
                        original = session.exec(select(MunicipalEvent).where(
                            MunicipalEvent.source == link.source, MunicipalEvent.event_id == link.event_id,
                            MunicipalEvent.call_id == receipt.call_id)).first()
                        # Trust provenance only when it belongs to the exact stored answer.
                        data = dict(record.data)
                        matches = original is not None and original.call_id == data.get('call_id') and all(
                            original.data.get(key) == data.get(key)
                            for key in ('user_text', 'assistant_text', 'answer_status'))
                        if matches and original is not None:
                            data['location_generations'] = original.data.get('location_generations')
                            data['user_location_generation'] = original.data.get('user_location_generation')
                            data['recommendation_event_ids'] = original.data.get('recommendation_event_ids')
                            data['sources'] = original.data.get('sources', [])
                        else:
                            data.pop('location_generations', None)
                            data.pop('user_location_generation', None)
                            data.pop('recommendation_event_ids', None)
                        data = redact_location_event(session, data)
                        if original is not None and original.data.get('user_text') == data.get('user_text'):
                            data['utterance_id'] = original.data.get('utterance_id')
                        if data.get('assistant_text'):
                            data['assistant_text'] = display_answer(data['assistant_text'])
                        entries.append(data)
                        seen_events.add(identity)
                utterance_links = session.exec(select(MunicipalHandoffUtterance).where(
                    MunicipalHandoffUtterance.handoff_id == receipt.id)).all()
                unanswered_utterances = []
                for utterance_link in utterance_links:
                    utterance = session.get(MunicipalEvent, utterance_link.municipal_event_id)
                    if utterance is not None and utterance.call_id == receipt.call_id and utterance.event_type == 'caller_utterance':
                        visible = redact_location_event(session, utterance.data)
                        unanswered_utterances.append({key: visible.get(key) for key in
                            ('event_id', 'utterance_id', 'occurred_at', 'user_text', 'phone')})
                privacy_links = session.exec(select(MunicipalHandoffPrivacy).where(
                    MunicipalHandoffPrivacy.handoff_id == receipt.id)).all()
                privacy_redactions = []
                for privacy_link in privacy_links:
                    privacy_event = session.get(MunicipalEvent, privacy_link.municipal_event_id)
                    if privacy_event is not None and privacy_event.call_id == receipt.call_id and privacy_event.event_type == 'privacy_redacted':
                        privacy_redactions.append({'event_id': privacy_event.event_id, 'occurred_at': privacy_event.occurred_at,
                            'user_text': '위치 정보가 포함될 수 있어 원문 확인 제한', 'phone': privacy_event.data.get('phone')})
                if entries or unanswered_utterances or privacy_redactions:
                    end = session.get(MunicipalHandoffEnd, receipt.id)
                    end_info = {'end_reason': end.end_reason, 'occurred_at': end.occurred_at} if end else None
                    case = session.get(MunicipalCase, receipt.call_id)
                    if case is not None:
                        live_end = session.exec(select(MunicipalEvent).where(
                            MunicipalEvent.call_id == receipt.call_id, MunicipalEvent.source == case.source,
                            MunicipalEvent.event_id == f'end:{receipt.call_id}',
                            MunicipalEvent.event_type == 'call_ended')).first()
                        if live_end is not None and live_end.data.get('end_reason'):
                            end_info = {'end_reason': live_end.data['end_reason'], 'occurred_at': live_end.occurred_at}
                    recommendations = []
                    links_reco = session.exec(select(MunicipalHandoffRecommendation).where(
                        MunicipalHandoffRecommendation.handoff_id == receipt.id)).all()
                    for reco_link in links_reco:
                        reco = session.get(MunicipalEvent, reco_link.municipal_event_id)
                        if reco is not None and reco.call_id == receipt.call_id and reco.event_type == 'recommendation':
                            visible = redact_location_event(session, reco.data).get('recommendation')
                            if isinstance(visible, dict):
                                recommendations.append({'event_id': reco.event_id, 'occurred_at': reco.occurred_at, **visible})
                    playback = session.exec(select(MunicipalHandoffPlayback).where(
                        MunicipalHandoffPlayback.handoff_id == receipt.id)).all()
                    handoffs.append({'handoff_id': receipt.id, 'handed_at': receipt.handed_at,
                                     'call_id': receipt.call_id, 'events': entries,
                                     'workflow': hospital_workflow(session, receipt.call_id),
                                     'call_end': end_info,
                                     'recommendations': recommendations,
                                     'unanswered_utterances': unanswered_utterances,
                                     'privacy_redactions': privacy_redactions,
                                     'playback_completed_event_ids': [event.event_id for event in playback]})
            return {'handoffs': handoffs}
