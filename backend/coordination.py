"""Synthetic municipality–hospital coordination; stores workflow independently of clinical charts."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field as InputField, model_validator
from sqlalchemy import Column, JSON
from sqlmodel import SQLModel, Field, Session, select

ROOT = Path(__file__).resolve().parents[1]
ASSIGNEES = ['미배정', '김하늘', '박서윤', '정다은']
STAGES = {'received': '접수', 'checking': '확인 중', 'hospital_requested': '병원 답변 대기',
          'response_received': '재안내 필요', 'resident_informed': '안내 완료', 'completed': '처리 완료'}
CATEGORIES = ['방문 절차', '이동 지원 확인', '준비사항 재확인', '연락 지원']


def safety_guidance():
    return json.loads((ROOT / 'backend/fixtures/safety-guidance.json').read_text())


class CoordinationCase(SQLModel, table=True):
    __tablename__ = 'coordination_cases'
    id: str = Field(primary_key=True)
    call_id: str = Field(foreign_key='calls.id', unique=True, index=True)
    status: str = Field(index=True)
    version: int = 1
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class CoordinationAction(SQLModel, table=True):
    __tablename__ = 'coordination_actions'
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key='coordination_cases.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


def now():
    return datetime.now(timezone.utc).isoformat()


def seed_coordination(engine, call_model):
    fixture = json.loads((ROOT / 'backend/fixtures/coordination.json').read_text())
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        for case in fixture['cases']:
            # Repeated startup must preserve staff edits and newly created cases.
            if session.exec(select(CoordinationCase).where(CoordinationCase.call_id == case['call_id'])).first():
                continue
            if not session.get(call_model, case['call_id']):
                continue
            session.add(CoordinationCase(id=case['id'], call_id=case['call_id'], status=case['status'], version=case['version'], data=case))
            session.flush()
            for action in fixture['actions']:
                if action['case_id'] == case['id']:
                    session.add(CoordinationAction(id=action['id'], case_id=case['id'], data=action))
        scenarios = json.loads((ROOT / 'backend/fixtures/safety-scenarios.json').read_text())
        for row in session.exec(select(CoordinationCase)).all():
            if 'safety' in row.data:
                continue
            scenario = scenarios.get(row.id) if row.status != 'completed' else None
            row.data = {**row.data, 'safety': scenario}
            row.version += 1
            session.add(row)
            if scenario:
                action = {'id': f'seed-safety-{row.id}', 'case_id': row.id, 'action_type': 'safety_opened',
                          'actor_name': '김하늘', 'actor_role': '지자체', 'note': scenario['report_text'],
                          'created_at': scenario['opened_at'], 'is_fixture': True}
                session.add(CoordinationAction(id=action['id'], case_id=row.id, data=action))
        session.commit()


def public_call(row):
    keys = ['name', 'age', 'phone', 'region', 'purpose', 'visit_type', 'started_at', 'ended_at',
            'call_status', 'planned_visit_text', 'summary_status', 'summary_text']
    return {'id': row.id, **{key: row.data.get(key) for key in keys},
            'call_status': row.call_status, 'started_at': row.started_at, 'data_mode': 'simulated'}


def case_dict(session, row, call_model):
    return {**row.data, 'id': row.id, 'call_id': row.call_id, 'status': row.status, 'version': row.version,
            'call': public_call(session.get(call_model, row.call_id))}


class NewCase(BaseModel):
    call_id: str = InputField(min_length=1, max_length=160)
    category: Literal['방문 절차', '이동 지원 확인', '준비사항 재확인', '연락 지원'] = '방문 절차'
    request_text: str = InputField(min_length=1, max_length=1500)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.request_text.strip():
            raise ValueError('주민 요청을 입력해 주세요.')
        return self


class CaseActionInput(BaseModel):
    action_type: Literal['metadata_updated', 'note_added', 'hospital_requested', 'contact_logged', 'completed',
                         'safety_opened', 'safety_updated', 'safety_contact_logged', 'safety_handoff_recorded', 'safety_closed']
    expected_version: int = InputField(ge=1)
    note: str = InputField(default='', max_length=1500)
    assignee: Literal['미배정', '김하늘', '박서윤', '정다은'] | None = None
    priority: Literal['normal', 'attention'] | None = None
    due_at: datetime | None = None
    outcome: Literal['connected', 'no_answer', 'callback_requested'] | None = None
    situation_type: Literal['health_concern', 'fall_report', 'unreachable', 'other'] | None = None
    location_text: str = InputField(default='', max_length=300)
    contact_target: Literal['119', '주민 본인', '보호자', '마을 담당자', '보건소', '병원'] | None = None
    contact_result: Literal['connected', 'no_answer'] | None = None
    resolution: Literal['handoff_confirmed', 'safe_confirmed'] | None = None

    @model_validator(mode='after')
    def validate_action(self):
        if self.due_at and self.due_at.tzinfo is None:
            raise ValueError('처리 기한에 시간대가 필요합니다.')
        if self.action_type != 'metadata_updated' and not self.note.strip():
            raise ValueError('처리 내용을 입력해 주세요.')
        if self.action_type == 'metadata_updated' and (self.assignee is None or self.priority is None):
            raise ValueError('담당자와 우선 확인 여부가 필요합니다.')
        if self.action_type == 'contact_logged' and self.outcome is None:
            raise ValueError('연락 결과를 선택해 주세요.')
        if self.action_type == 'safety_opened' and self.situation_type is None:
            raise ValueError('담당자가 확인할 상황 유형을 선택해 주세요.')
        if self.action_type == 'safety_contact_logged' and (self.contact_target is None or self.contact_result is None):
            raise ValueError('실제 연락한 대상과 연결 결과가 필요합니다.')
        if self.action_type == 'safety_closed' and self.resolution is None:
            raise ValueError('후속 확인을 마친 근거가 필요합니다.')
        return self


class HospitalResponseInput(BaseModel):
    expected_version: int = InputField(ge=1)
    response_text: str = InputField(min_length=1, max_length=1500)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.response_text.strip():
            raise ValueError('병원 답변을 입력해 주세요.')
        return self


def register_coordination_routes(app, engine, call_model, message_model, location_model):
    @app.get('/municipality', response_class=HTMLResponse)
    @app.get('/municipality/', response_class=HTMLResponse, include_in_schema=False)
    def municipality_dashboard():
        html = (ROOT / 'dist/municipality.html').read_text()
        return HTMLResponse(html.replace('<head>', '<head><meta name="municipality-data-source" content="api">', 1),
                            headers={'Cache-Control': 'no-store'})

    @app.get('/api/municipality/bootstrap')
    def municipality_bootstrap():
        with Session(engine) as session:
            cases = [case_dict(session, row, call_model) for row in session.exec(select(CoordinationCase)).all()]
            cases.sort(key=lambda c: (c['updated_at'], c['id']), reverse=True)
            return {'data_mode': 'simulated', 'storage': 'sqlite', 'municipality': '화순군',
                    'assignees': ASSIGNEES, 'stages': STAGES, 'categories': CATEGORIES, 'cases': cases,
                    'safety_guidance': safety_guidance(),
                    'calls': [public_call(c) for c in session.exec(select(call_model).order_by(call_model.started_at.desc())).all()],
                    'actions': [r.data for r in session.exec(select(CoordinationAction)).all()]}

    @app.get('/api/municipality/cases/{case_id}')
    def municipality_case(case_id: str):
        with Session(engine) as session:
            row = session.get(CoordinationCase, case_id)
            if not row:
                raise HTTPException(404, '연계 업무가 없습니다.')
            return {'case': case_dict(session, row, call_model),
                    'messages': [r.data for r in session.exec(select(message_model).where(message_model.call_id == row.call_id).order_by(message_model.sequence)).all()],
                    'locations': [r.data for r in session.exec(select(location_model).where(location_model.call_id == row.call_id)).all()],
                    'actions': [r.data for r in session.exec(select(CoordinationAction).where(CoordinationAction.case_id == row.id)).all()]}

    @app.get('/api/municipality/calls/{call_id}')
    def municipality_call(call_id: str):
        with Session(engine) as session:
            row = session.get(call_model, call_id)
            if not row:
                raise HTTPException(404, '통화가 없습니다.')
            return {'call': public_call(row), 'actions': [],
                    'messages': [r.data for r in session.exec(select(message_model).where(message_model.call_id == call_id).order_by(message_model.sequence)).all()],
                    'locations': [r.data for r in session.exec(select(location_model).where(location_model.call_id == call_id)).all()]}

    @app.post('/api/municipality/cases')
    def create_case(body: NewCase):
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            call = session.get(call_model, body.call_id)
            if not call:
                raise HTTPException(404, '통화가 없습니다.')
            if session.exec(select(CoordinationCase).where(CoordinationCase.call_id == body.call_id)).first():
                raise HTTPException(409, '이 통화에는 이미 연계 업무가 등록되어 있습니다.')
            at, case_id = now(), f'case-{uuid4()}'
            data = {'id': case_id, 'call_id': call.id, 'status': 'received', 'version': 1,
                    'category': body.category, 'request_text': body.request_text.strip(), 'assignee': '미배정',
                    'priority': 'normal', 'due_at': None, 'hospital_name': '김장철병원', 'hospital_request': '',
                    'hospital_response': '', 'last_contact_outcome': None, 'created_at': at, 'updated_at': at,
                    'support': {'living': '미확인', 'device': '미확인', 'mobility': '미확인', 'companion': '미확인'},
                    'data_mode': 'simulated', 'safety': None}
            row = CoordinationCase(id=case_id, call_id=call.id, status='received', data=data)
            session.add(row)
            session.flush()
            action = {'id': str(uuid4()), 'case_id': case_id, 'action_type': 'case_created',
                      'actor_name': '김하늘', 'actor_role': '지자체', 'note': body.request_text.strip(), 'created_at': at}
            session.add(CoordinationAction(id=action['id'], case_id=case_id, data=action))
            session.commit()
            return {'case': case_dict(session, row, call_model), 'action': action}

    @app.post('/api/municipality/cases/{case_id}/actions')
    def municipality_action(case_id: str, body: CaseActionInput):
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = session.get(CoordinationCase, case_id)
            if not row:
                raise HTTPException(404, '연계 업무가 없습니다.')
            if row.version != body.expected_version:
                raise HTTPException(409, '다른 담당자가 변경했습니다. 최신 기록을 불러온 뒤 다시 저장해 주세요.')
            if row.status == 'completed' and body.action_type != 'note_added' and not body.action_type.startswith('safety_'):
                raise HTTPException(409, '완료된 업무에는 추가 메모만 남길 수 있습니다.')
            data, at = dict(row.data), now()
            action = {'id': str(uuid4()), 'case_id': row.id, 'action_type': body.action_type,
                      'actor_name': '김하늘', 'actor_role': '지자체', 'note': body.note.strip(), 'created_at': at,
                      'previous_status': row.status}
            safety = data.get('safety')
            active_safety = safety and safety['status'] != 'closed'
            if active_safety and body.action_type in {'completed', 'hospital_requested'}:
                raise HTTPException(409, '위기 대응을 먼저 확인해 주세요. 일반 방문 업무 완료·전달은 후속 확인 이후 진행합니다.')
            if body.action_type.startswith('safety_'):
                if body.action_type == 'safety_opened':
                    if active_safety:
                        raise HTTPException(409, '이미 진행 중인 위기 대응이 있습니다.')
                    action['previous_safety'] = safety
                    safety = {'status': 'checking', 'category': body.situation_type,
                              'report_text': body.note.strip(), 'location_text': body.location_text.strip() or '현재 위치 미확인',
                              'reported_by': '담당자 직접 접수', 'opened_at': at}
                else:
                    if not active_safety:
                        raise HTTPException(409, '진행 중인 위기 대응이 없습니다.')
                    safety = dict(safety)
                    if body.action_type == 'safety_updated':
                        safety.update(location_text=body.location_text.strip() or '현재 위치 미확인',
                                      latest_situation=body.note.strip())
                    elif body.action_type == 'safety_contact_logged':
                        safety.update(last_contact_target=body.contact_target, last_contact_result=body.contact_result,
                                      last_contact_note=body.note.strip(), last_contact_at=at)
                        action.update(contact_target=body.contact_target, contact_result=body.contact_result)
                        if body.contact_result == 'connected' and safety['status'] == 'checking':
                            safety['status'] = 'contacted'
                    elif body.action_type == 'safety_handoff_recorded':
                        if safety.get('last_contact_result') != 'connected':
                            raise HTTPException(409, '실제 연결된 연락 기록을 먼저 남겨 주세요.')
                        safety.update(status='handed_over', handoff_at=at, handoff_note=body.note.strip(),
                                      handoff_target=safety['last_contact_target'])
                    elif body.action_type == 'safety_closed':
                        if body.resolution == 'handoff_confirmed' and safety['status'] != 'handed_over':
                            raise HTTPException(409, '관계기관 인계 확인을 먼저 기록해 주세요.')
                        if body.resolution == 'safe_confirmed' and not (
                                safety.get('last_contact_result') == 'connected' and safety.get('last_contact_target') in {'주민 본인', '보호자'}):
                            raise HTTPException(409, '주민 본인 또는 보호자에게 직접 확인한 연락 기록이 필요합니다.')
                        safety.update(status='closed', resolution=body.resolution, closed_at=at, resolution_note=body.note.strip())
                safety['updated_at'] = at
                data['safety'] = safety
                action['safety_status'] = safety['status']
            if body.action_type == 'metadata_updated':
                data.update(assignee=body.assignee, priority=body.priority,
                            due_at=body.due_at.isoformat() if body.due_at else None)
                action['note'] = f"담당자 {body.assignee} · {'우선 확인' if body.priority == 'attention' else '일반'} · 기한 {data['due_at'] or '미지정'}"
                if row.status == 'received':
                    row.status = 'checking'
            elif body.action_type == 'hospital_requested':
                if row.status not in {'received', 'checking'}:
                    raise HTTPException(409, '접수·확인 중인 업무만 병원에 전달할 수 있습니다.')
                if data['assignee'] == '미배정':
                    raise HTTPException(409, '담당자를 먼저 배정해 주세요.')
                data.update(hospital_request=body.note.strip(), hospital_requested_at=at)
                row.status = 'hospital_requested'
            elif body.action_type == 'contact_logged':
                data.update(last_contact_outcome=body.outcome, last_contact_at=at, last_contact_note=body.note.strip())
                action['outcome'] = body.outcome
                if body.outcome == 'connected' and row.status == 'response_received':
                    row.status = 'resident_informed'
            elif body.action_type == 'completed':
                if row.status != 'resident_informed':
                    raise HTTPException(409, '병원 답변을 주민에게 안내한 뒤 완료할 수 있습니다.')
                row.status = 'completed'
            row.version += 1
            row.data = {**data, 'status': row.status, 'updated_at': at, 'version': row.version}
            action['new_status'] = row.status
            session.add(row)
            session.add(CoordinationAction(id=action['id'], case_id=row.id, data=action))
            session.commit()
            return {'case': case_dict(session, row, call_model), 'action': action}

    @app.get('/api/team/hospital/referrals')
    def hospital_referrals():
        with Session(engine) as session:
            rows = [case_dict(session, r, call_model) for r in session.exec(select(CoordinationCase)).all() if r.data.get('hospital_request')]
            return {'referrals': sorted(rows, key=lambda c: (c['status'] != 'hospital_requested', c['updated_at']))}

    @app.post('/api/team/hospital/referrals/{case_id}/responses')
    def hospital_response(case_id: str, body: HospitalResponseInput):
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = session.get(CoordinationCase, case_id)
            if not row:
                raise HTTPException(404, '연계 요청이 없습니다.')
            if row.version != body.expected_version or row.status != 'hospital_requested':
                raise HTTPException(409, '이미 답변했거나 변경된 요청입니다. 최신 기록을 확인해 주세요.')
            at = now()
            row.status = 'response_received'
            row.version += 1
            row.data = {**row.data, 'hospital_response': body.response_text.strip(), 'hospital_responded_at': at,
                        'updated_at': at, 'status': row.status, 'version': row.version}
            action = {'id': str(uuid4()), 'case_id': row.id, 'action_type': 'hospital_responded',
                      'actor_name': '이지은', 'actor_role': '병원', 'note': body.response_text.strip(),
                      'previous_status': 'hospital_requested', 'new_status': row.status, 'created_at': at}
            session.add(row)
            session.add(CoordinationAction(id=action['id'], case_id=row.id, data=action))
            session.commit()
            return {'case': case_dict(session, row, call_model), 'action': action}
