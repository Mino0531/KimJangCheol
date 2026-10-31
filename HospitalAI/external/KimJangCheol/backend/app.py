"""Local-only synthetic hospital demo. Never connects to a clinical system."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4
import json
import os
import hashlib

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field as InputField, model_validator
from sqlalchemy import Column, JSON, UniqueConstraint, event as sqlalchemy_event
from sqlmodel import Field, Session, SQLModel, create_engine, select
from backend.chart_schema import ChartBundle
from backend.knowledge import import_knowledge, register_knowledge_routes, EXTRA_SOURCE
from backend.preparation import seed_preparation_policies, register_preparation_routes
from backend.consultations import register_consultation_routes
from backend.recommendations import register_recommendation_routes
from backend.municipal_intake import initialize_municipal_schema, register_municipal_intake_routes
from backend.municipal_handoff import register_municipal_handoff_routes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(__file__).resolve().parent / '.local-data' / 'hospital.sqlite3'


class Patient(SQLModel, table=True):
    __tablename__ = 'patients'
    id: str = Field(primary_key=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Condition(SQLModel, table=True):
    __tablename__ = 'patient_conditions'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Medication(SQLModel, table=True):
    __tablename__ = 'patient_medications'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Prescription(SQLModel, table=True):
    __tablename__ = 'patient_prescriptions'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Observation(SQLModel, table=True):
    __tablename__ = 'patient_observations'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Encounter(SQLModel, table=True):
    __tablename__ = 'patient_encounters'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Call(SQLModel, table=True):
    __tablename__ = 'calls'
    id: str = Field(primary_key=True)
    patient_id: str | None = Field(default=None, foreign_key='patients.id', index=True)
    caller_key: str = Field(index=True)
    started_at: str = Field(index=True)
    call_status: str = Field(index=True)
    review_status: str = Field(index=True)
    version: int = 1
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Location(SQLModel, table=True):
    __tablename__ = 'call_locations'
    id: str = Field(primary_key=True)
    call_id: str = Field(foreign_key='calls.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class Message(SQLModel, table=True):
    __tablename__ = 'call_messages'
    __table_args__ = (UniqueConstraint('call_id', 'sequence'),)
    id: str = Field(primary_key=True)
    call_id: str = Field(foreign_key='calls.id', index=True)
    sequence: int
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class CallEvent(SQLModel, table=True):
    __tablename__ = 'call_events'
    __table_args__ = (UniqueConstraint('source', 'event_id'),)
    id: str = Field(primary_key=True)
    call_id: str = Field(foreign_key='calls.id', index=True)
    source: str
    event_id: str
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class DeskAction(SQLModel, table=True):
    __tablename__ = 'desk_actions'
    id: str = Field(primary_key=True)
    call_id: str = Field(foreign_key='calls.id', index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class ChartImport(SQLModel, table=True):
    __tablename__ = 'chart_imports'
    id: str = Field(primary_key=True)
    imported_at: str
    patient_count: int
    source_name: str


class ChartRevision(SQLModel, table=True):
    __tablename__ = 'chart_revisions'
    id: str = Field(primary_key=True)
    patient_id: str = Field(foreign_key='patients.id', index=True)
    import_id: str = Field(foreign_key='chart_imports.id', index=True)
    imported_at: str = Field(index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))
    previous: dict = Field(sa_column=Column(JSON, nullable=False))


PATIENT_CHILDREN = [('conditions', Condition), ('current_medications', Medication),
                    ('prescriptions', Prescription), ('observations', Observation), ('encounters', Encounter)]
CALL_FIELDS = ['patient_id', 'caller_key', 'started_at', 'call_status', 'review_status', 'version']


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def call_dict(row):
    return {**row.data, 'id': row.id, **{key: getattr(row, key) for key in CALL_FIELDS}}


def patient_dict(session, patient):
    result = {**patient.data, 'id': patient.id}
    for key, model in PATIENT_CHILDREN:
        result[key] = [row.data for row in session.exec(select(model).where(model.patient_id == patient.id).order_by(model.id)).all()]
    result['medication_history'] = result['current_medications']
    result['current_medications'] = [m for m in result['medication_history'] if m.get('status_code', 'active') == 'active']
    result['prescriptions'].sort(key=lambda r: r['date'], reverse=True)
    result['observations'].sort(key=lambda r: r['date'])
    result['encounters'].sort(key=lambda r: r['date'], reverse=True)
    return result


def project_chart(chart):
    """Canonical exchange record → existing dashboard view model; no clinical inference."""
    p = chart.model_dump(mode='json')
    birth = chart.identity.birth_date
    as_of = chart.provenance.as_of
    age = as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))
    a = chart.allergies
    labels = {'recorded': '기록 있음', 'none_reported': '없다고 보고됨', 'unknown': '미확인'}
    allergy_text = '; '.join(x.substance + (f' · {x.reaction}' if x.reaction else '') for x in a.items) if a.items else '알레르기가 없다고 보고한 가상 기록입니다.' if a.knowledge_status == 'none_reported' else '알레르기 확인 기록이 없습니다.'
    source = chart.provenance.source_label
    return {
        'id': chart.patient_id, 'chart_number': chart.chart_number, 'name': chart.identity.name, 'age': age,
        'sex': {'female': '여', 'male': '남', 'unknown': '미확인'}[chart.identity.sex],
        'identity': p['identity'], 'source': source, 'as_of': str(as_of), 'provenance': p['provenance'],
        'format_id': 'KJC_VISIT_PREPARATION_CHART', 'schema_version': '1.0', 'is_fictional': True,
        'knowledge_status': {'conditions': chart.conditions.knowledge_status, 'medications': chart.medications.knowledge_status},
        'visit_preparation': p['visit_preparation'],
        'allergies': {'status': labels[a.knowledge_status], 'knowledge_status': a.knowledge_status, 'text': allergy_text},
        'conditions': [{'id': x.condition_id, 'name': x.name, 'since': f'{x.diagnosed_on.year}년 기록' if x.diagnosed_on else '기록 시점 미확인',
                        'status': x.status, 'source': source, 'note': x.note} for x in chart.conditions.items],
        'current_medications': [{'id': m.medication_id, 'name': m.name, 'purpose': m.purpose, 'started_on': str(m.started_on) if m.started_on else None,
                                  'ended_on': str(m.ended_on) if m.ended_on else None,
                                  'status': {'active': '복용 중', 'stopped': '복용 중단', 'unknown': '복용 여부 미확인'}[m.status],
                                  'status_code': m.status, 'source': source, 'is_fictional': True} for m in chart.medications.items],
        'prescriptions': [{'id': rx.prescription_id, 'date': str(rx.prescribed_on), 'department': rx.department, 'provider': rx.institution,
                           'medications': [m.medication_name for m in rx.items], 'medication_ids': [m.medication_id for m in rx.items], 'status': '이전 처방 기록'} for rx in chart.prescriptions],
        'observations': [{'id': o.observation_id, 'date': str(o.observed_on), 'systolic': o.systolic, 'diastolic': o.diastolic, 'glucose': o.glucose,
                          'blood_pressure_unit': o.blood_pressure_unit, 'glucose_unit': o.glucose_unit, 'glucose_context': o.glucose_context} for o in chart.observations],
        'encounters': [{'id': e.encounter_id, 'date': str(e.visited_on), 'department': e.department, 'provider': e.institution, 'title': e.title, 'note': e.note} for e in chart.encounters]
    }


def import_chart_bundle(engine, bundle: ChartBundle, source_name='api'):
    canonical = bundle.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    with Session(engine) as session:
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        existing = session.get(ChartImport, digest)
        if existing:
            return {'applied': False, 'duplicate': True, 'import_id': digest, 'patient_count': existing.patient_count}
        at = timestamp()
        # Preflight every chart before writing: no partial batch imports or stale overwrites.
        for chart in bundle.patients:
            existing_patient = session.get(Patient, chart.patient_id)
            if existing_patient:
                stored = existing_patient.data
                if stored.get('identity', {}).get('name', chart.identity.name) != chart.identity.name:
                    raise ValueError(f'{chart.patient_id}: 같은 환자 ID의 이름이 다릅니다.')
                if stored.get('identity', {}).get('birth_date', str(chart.identity.birth_date)) != str(chart.identity.birth_date):
                    raise ValueError(f'{chart.patient_id}: 같은 환자 ID의 생년월일이 다릅니다.')
                if stored.get('as_of', '') > str(chart.provenance.as_of):
                    raise ValueError(f'{chart.patient_id}: 현재 자료보다 과거의 차트입니다.')
                previous_at = stored.get('provenance', {}).get('recorded_at')
                if previous_at and datetime.fromisoformat(previous_at) > chart.provenance.recorded_at:
                    raise ValueError(f'{chart.patient_id}: 더 최근에 등록된 기록이 있습니다.')
                if stored.get('chart_number') != chart.chart_number:
                    raise ValueError(f'{chart.patient_id}: 차트번호가 일치하지 않습니다.')
            same_number = session.exec(select(Patient)).all()
            if any(p.id != chart.patient_id and p.data.get('chart_number') == chart.chart_number for p in same_number):
                raise ValueError(f'{chart.patient_id}: 다른 환자가 사용하는 차트번호입니다.')
            linked = session.exec(select(Call).where(Call.patient_id == chart.patient_id)).all()
            if any(c.data.get('name') != chart.identity.name for c in linked):
                raise ValueError(f'{chart.patient_id}: 연결된 통화의 가상 환자 이름과 다릅니다.')
        session.add(ChartImport(id=digest, imported_at=at, patient_count=len(bundle.patients), source_name=source_name))
        session.flush()
        for chart in bundle.patients:
            projected = project_chart(chart)
            patient = session.get(Patient, chart.patient_id)
            previous = patient_dict(session, patient) if patient else {}
            base = {k: v for k, v in projected.items() if k not in {x[0] for x in PATIENT_CHILDREN} | {'id'}}
            base['chart_import_id'] = digest
            if patient:
                patient.data = base
            else:
                patient = Patient(id=chart.patient_id, data=base)
            session.add(patient)
            session.flush()
            for key, model in PATIENT_CHILDREN:
                old_rows = {r.id: r for r in session.exec(select(model).where(model.patient_id == chart.patient_id)).all()}
                for i, record in enumerate(projected[key]):
                    row_id = f'{chart.patient_id}-{key}-{i:03d}'
                    row = old_rows.pop(row_id, None)
                    if row:
                        row.data = record
                    else:
                        row = model(id=row_id, patient_id=chart.patient_id, data=record)
                    session.add(row)
                # The supplied chart is a complete snapshot; its prior snapshot is retained below.
                for obsolete in old_rows.values():
                    session.delete(obsolete)
            session.add(ChartRevision(id=f'{digest}:{chart.patient_id}', patient_id=chart.patient_id, import_id=digest,
                                      imported_at=at, data=chart.model_dump(mode='json'), previous=previous))
        session.commit()
    return {'applied': True, 'duplicate': False, 'import_id': digest, 'patient_count': len(bundle.patients)}


def seed_database(engine):
    fixture = json.loads((ROOT / 'backend/fixtures/demo.json').read_text(encoding='utf-8'))
    with Session(engine) as session:
        # Seed only a pristine DB: server restarts never overwrite staff edits or existing records.
        if session.exec(select(Patient).limit(1)).first() or session.exec(select(Call).limit(1)).first():
            return
        for p in fixture['patients']:
            base = {k: v for k, v in p.items() if k not in {x[0] for x in PATIENT_CHILDREN} | {'id'}}
            session.add(Patient(id=p['id'], data=base))
        session.flush()
        for p in fixture['patients']:
            for key, model in PATIENT_CHILDREN:
                for i, value in enumerate(p[key]):
                    session.add(model(id=f"{p['id']}-{key}-{i:03d}", patient_id=p['id'], data=value))
        for c in fixture['calls']:
            session.add(Call(id=c['id'], data=c, **{k: c.get(k) for k in CALL_FIELDS}))
        session.flush()
        for key, model in [('call_locations', Location), ('call_messages', Message), ('call_events', CallEvent), ('desk_actions', DeskAction)]:
            for row in fixture[key]:
                extra = {'sequence': row['sequence']} if model is Message else {'source': row['source'], 'event_id': row['event_id']} if model is CallEvent else {}
                session.add(model(id=row['id'], call_id=row['call_id'], data=row, **extra))
        session.commit()


class ActionInput(BaseModel):
    action_type: Literal['note_added', 'review_changed']
    expected_version: int = InputField(ge=1)
    note: str | None = InputField(default=None, max_length=800)
    new_status: Literal['unreviewed', 'reviewing', 'reviewed'] | None = None

    @model_validator(mode='after')
    def check_action(self):
        if self.action_type == 'note_added' and not (self.note or '').strip():
            raise ValueError('메모를 입력해 주세요.')
        if self.action_type == 'review_changed' and self.new_status is None:
            raise ValueError('확인 상태가 필요합니다.')
        return self


class EventInput(BaseModel):
    source: str = InputField(min_length=1, max_length=80)
    event_id: str = InputField(min_length=1, max_length=160)
    call_id: str = InputField(min_length=1, max_length=160)
    event_type: Literal['call_started', 'message_finalized', 'location_observed', 'call_ended', 'summary_ready']
    occurred_at: datetime
    payload: dict = InputField(default_factory=dict)

    @model_validator(mode='after')
    def timezone_required(self):
        if self.occurred_at.tzinfo is None:
            raise ValueError('시간대가 포함된 occurred_at이 필요합니다.')
        return self


class CallStarted(BaseModel):
    patient_id: str | None = None
    caller_key: str = InputField(min_length=1, max_length=160)
    name: str = InputField(min_length=1, max_length=100)
    age: int = InputField(ge=0, le=120)
    region: str = InputField(max_length=200)
    purpose: str = InputField(max_length=200)
    visit_type: str = InputField(max_length=100)
    color: str = InputField(default='peach', pattern='^(peach|mint|blue|violet)$')
    phone: str = InputField(max_length=40)
    hospital_code: Literal['KJC_DEMO']
    planned_visit_text: str = InputField(default='', max_length=300)


class MessageInput(BaseModel):
    sequence: int = InputField(ge=1)
    speaker: Literal['caller', 'assistant']
    text: str = InputField(min_length=1, max_length=4000)


class LocationInput(BaseModel):
    latitude: float | None = InputField(default=None, ge=-90, le=90)
    longitude: float | None = InputField(default=None, ge=-180, le=180)
    location_source: Literal['simulated']
    accuracy_m: float | None = InputField(default=None, ge=0)

    @model_validator(mode='after')
    def pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('위도와 경도는 함께 입력해야 합니다.')
        return self


class SummaryInput(BaseModel):
    summary_text: str = InputField(min_length=1, max_length=4000)


def create_app(db_path: Path = DEFAULT_DB):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f'sqlite:///{db_path}', connect_args={'check_same_thread': False, 'timeout': 10})

    @sqlalchemy_event.listens_for(engine, 'connect')
    def pragmas(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')

    @asynccontextmanager
    async def lifespan(_):
        SQLModel.metadata.create_all(engine)
        initialize_municipal_schema(engine)
        seed_database(engine)
        bundle = ChartBundle.model_validate_json((ROOT / 'backend/fixtures/charts-v1.json').read_text(encoding='utf-8'))
        import_chart_bundle(engine, bundle, source_name='charts-v1.json')
        import_knowledge(engine)
        import_knowledge(engine, EXTRA_SOURCE, source_kind='service_authored')
        seed_preparation_policies(engine)
        yield
        engine.dispose()

    app = FastAPI(title='김장철병원 · 가상 환자 DB API', version='1.0.0', lifespan=lifespan)
    app.state.engine = engine
    register_knowledge_routes(app, engine)
    register_preparation_routes(app, engine, Call, Message, Patient, call_dict, patient_dict)
    register_consultation_routes(app, engine)
    register_recommendation_routes(app, engine)
    register_municipal_intake_routes(app, engine)
    register_municipal_handoff_routes(app, engine)

    @app.get('/api/team/chart-format')
    def chart_format():
        return {'format_id': 'KJC_VISIT_PREPARATION_CHART', 'schema_version': '1.0',
                'data_classification': 'synthetic', 'official_medical_standard': False,
                'schema': ChartBundle.model_json_schema()}

    @app.get('/api/team/chart-template')
    def chart_template():
        return json.loads((ROOT / 'backend/schemas/patient-chart-v1.template.json').read_text(encoding='utf-8'))

    @app.post('/api/team/chart-imports')
    def chart_import(body: ChartBundle):
        try:
            return import_chart_bundle(engine, body)
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get('/api/team/chart-imports')
    def chart_import_list():
        with Session(engine) as session:
            return [row.model_dump() for row in session.exec(select(ChartImport).order_by(ChartImport.imported_at.desc()).limit(50)).all()]

    @app.get('/api/team/patients/{patient_id}/chart/export')
    def chart_export(patient_id: str):
        with Session(engine) as session:
            patient = session.get(Patient, patient_id)
            if not patient:
                raise HTTPException(404, '환자 차트가 없습니다.')
            revision = session.get(ChartRevision, f"{patient.data.get('chart_import_id')}:{patient_id}")
            if not revision:
                raise HTTPException(404, '공통 양식으로 가져온 기록이 없습니다.')
            return {'format_id': 'KJC_VISIT_PREPARATION_CHART', 'schema_version': '1.0', 'hospital_code': 'KJC_DEMO',
                    'data_classification': 'synthetic', 'generated_at': timestamp(), 'patients': [revision.data]}

    @app.get('/', response_class=HTMLResponse)
    def dashboard():
        html = (ROOT / 'dist/index.html').read_text(encoding='utf-8')
        return HTMLResponse(html.replace('<head>', '<head><meta name="hospital-data-source" content="api">', 1), headers={'Cache-Control': 'no-store'})

    @app.get('/hospital-reference', response_class=HTMLResponse)
    @app.get('/hospital-reference/', response_class=HTMLResponse, include_in_schema=False)
    def hospital_reference():
        html = (ROOT / 'dist/hospital-reference.html').read_text(encoding='utf-8')
        return HTMLResponse(html, headers={'Cache-Control': 'no-store'})

    @app.get('/municipality', response_class=HTMLResponse)
    @app.get('/municipality/', response_class=HTMLResponse, include_in_schema=False)
    def municipal_dashboard():
        html = (ROOT / 'dist/municipality.html').read_text(encoding='utf-8')
        return HTMLResponse(html, headers={'Cache-Control': 'no-store'})

    @app.get('/favicon.ico', include_in_schema=False)
    def favicon():
        from fastapi.responses import Response
        return Response(status_code=204)

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'storage': 'sqlite', 'data_mode': 'simulated', 'clinical_system_connected': False}

    @app.get('/api/team/bootstrap')
    def bootstrap():
        with Session(engine) as session:
            result = {'schema_version': 1, 'chart_fixture_version': 2, 'storage': 'sqlite',
                      'preparation_revision': hashlib.sha256((EXTRA_SOURCE.parent / 'preparation-policies.json').read_bytes() + (EXTRA_SOURCE.parent / 'vocabulary-levels.json').read_bytes() + EXTRA_SOURCE.read_bytes()).hexdigest(),
                      'patients': [patient_dict(session, p) for p in session.exec(select(Patient).order_by(Patient.id)).all()],
                      'calls': [call_dict(c) for c in session.exec(select(Call).order_by(Call.started_at.desc())).all()]}
            for key, model in [('call_locations', Location), ('call_messages', Message), ('call_events', CallEvent), ('desk_actions', DeskAction)]:
                result[key] = [r.data for r in session.exec(select(model)).all()]
            return result

    @app.get('/api/team/patients/{patient_id}/chart')
    def chart(patient_id: str):
        with Session(engine) as session:
            patient = session.get(Patient, patient_id)
            if not patient:
                raise HTTPException(404, '환자 차트가 없습니다.')
            return patient_dict(session, patient)

    @app.get('/api/team/calls')
    def calls(caller_key: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        with Session(engine) as session:
            q = select(Call)
            if caller_key:
                q = q.where(Call.caller_key == caller_key)
            return [call_dict(c) for c in session.exec(q.order_by(Call.started_at.desc(), Call.id).offset(offset).limit(limit)).all()]

    @app.get('/api/team/calls/{call_id}')
    def call_detail(call_id: str):
        with Session(engine) as session:
            row = session.get(Call, call_id)
            if not row:
                raise HTTPException(404, '통화가 없습니다.')
            result = {'call': call_dict(row), 'patient': patient_dict(session, p) if row.patient_id and (p := session.get(Patient, row.patient_id)) else None}
            for key, model in [('locations', Location), ('messages', Message), ('actions', DeskAction)]:
                rows = session.exec(select(model).where(model.call_id == call_id)).all()
                result[key] = [r.data for r in rows]
            result['messages'].sort(key=lambda m: m['sequence'])
            return result

    @app.post('/api/team/calls/{call_id}/desk-actions')
    def desk_action(call_id: str, body: ActionInput):
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = session.get(Call, call_id)
            if not row:
                raise HTTPException(404, '통화가 없습니다.')
            if row.version != body.expected_version:
                raise HTTPException(409, '다른 변경이 저장됐습니다. 최신 기록을 다시 확인해 주세요.')
            action = {'id': str(uuid4()), 'call_id': call_id, 'actor_key': 'demo-staff-01', 'actor_name': '이지은',
                      'action_type': body.action_type, 'created_at': timestamp()}
            if body.action_type == 'note_added':
                action['note'] = body.note.strip()
            else:
                action.update(previous_status=row.review_status, new_status=body.new_status)
                row.review_status = body.new_status
            row.version += 1
            session.add(row)
            session.add(DeskAction(id=action['id'], call_id=call_id, data=action))
            session.commit()
            return {'action': action, 'call': call_dict(row)}

    @app.post('/api/team/call-events')
    def receive_event(body: EventInput):
        event_data = body.model_dump(mode='json')
        payload_models = {'call_started': CallStarted, 'message_finalized': MessageInput,
                          'location_observed': LocationInput, 'summary_ready': SummaryInput}
        try:
            p = payload_models[body.event_type].model_validate(body.payload).model_dump() if body.event_type in payload_models else {}
        except ValueError as error:
            raise HTTPException(422, str(error))
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            existing = session.exec(select(CallEvent).where(CallEvent.source == body.source, CallEvent.event_id == body.event_id)).first()
            if existing:
                if existing.data.get('request') != event_data:
                    raise HTTPException(409, '같은 이벤트 ID에 다른 내용이 있습니다.')
                return {'applied': False, 'duplicate': True, 'call_id': body.call_id}
            row = session.get(Call, body.call_id)
            at = body.occurred_at.isoformat()
            if body.event_type == 'call_started':
                if row:
                    raise HTTPException(409, '이미 존재하는 통화입니다.')
                if p['patient_id'] and not session.get(Patient, p['patient_id']):
                    raise HTTPException(422, '명시한 환자 ID가 없습니다.')
                data = {**p, 'id': body.call_id, 'started_at': at, 'ended_at': None, 'call_status': 'in_progress',
                        'review_status': 'unreviewed', 'summary_status': 'pending', 'summary_text': '',
                        'data_mode': 'simulated', 'ai_mode': None, 'summary_source': 'fixture', 'version': 1}
                row = Call(id=body.call_id, data=data, **{k: data[k] for k in CALL_FIELDS})
                session.add(row)
                session.flush()
            elif not row:
                raise HTTPException(409, '통화 시작 이벤트가 먼저 필요합니다.')
            elif body.event_type == 'message_finalized':
                if session.exec(select(Message).where(Message.call_id == row.id, Message.sequence == p['sequence'])).first():
                    raise HTTPException(409, '이미 저장된 발화 순서입니다.')
                data = {'id': str(uuid4()), 'call_id': row.id, **p, 'occurred_at': at}
                session.add(Message(id=data['id'], call_id=row.id, sequence=p['sequence'], data=data))
            elif body.event_type == 'location_observed':
                data = {'id': str(uuid4()), 'call_id': row.id, **p, 'observed_at': at}
                session.add(Location(id=data['id'], call_id=row.id, data=data))
            elif body.event_type == 'call_ended':
                if row.call_status != 'in_progress':
                    raise HTTPException(409, '이미 종료된 통화입니다.')
                row.call_status = 'ended'
                row.data = {**row.data, 'ended_at': at}
            elif body.event_type == 'summary_ready':
                row.data = {**row.data, 'summary_status': 'ready', 'summary_text': p['summary_text'], 'summary_source': 'fixture', 'ai_mode': None}
            row.version += 1
            session.add(row)
            data = {**event_data, 'id': str(uuid4()), 'received_at': timestamp(), 'request': event_data}
            session.add(CallEvent(id=data['id'], call_id=row.id, source=body.source, event_id=body.event_id, data=data))
            session.commit()
            return {'applied': True, 'call_id': row.id, 'call': call_dict(row)}

    return app


app = create_app(Path(os.environ.get('HOSPITAL_DB_PATH', DEFAULT_DB)))
