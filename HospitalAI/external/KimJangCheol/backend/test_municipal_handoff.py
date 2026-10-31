"""Hospital receives only explicitly reviewed municipal consultations."""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend.consultations import ConsultationRecord, register_consultation_routes
from backend.municipal_intake import MunicipalCase, MunicipalEvent
from backend.municipal_handoff import register_municipal_handoff_routes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'municipal-test')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'hospital-test')
    engine = create_engine('sqlite:///' + str(tmp_path / 'handoff.sqlite'),
                           connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    register_consultation_routes(app, engine)
    register_municipal_handoff_routes(app, engine)
    yield TestClient(app), engine
    engine.dispose()


def add_call(engine, *, status='acknowledged', unread=False, urgent=False, user_text='무릎이 불편해요'):
    occurred = datetime.now(timezone.utc).isoformat()
    payload = {'source': 'hospital-ai', 'event_id': 'turn:turn-1', 'call_id': 'call-1',
               'occurred_at': occurred, 'event_type': 'urgent' if urgent else 'turn_completed',
               'phone': '010-0000-0000', 'user_text': user_text, 'assistant_text': '의료진과 확인하세요.',
               'answer_status': 'emergency' if urgent else 'completed', 'sources': []}
    with Session(engine) as session:
        session.add(MunicipalCase(call_id='call-1', source='hospital-ai', phone='010-0000-0000', created_at=occurred,
                                  updated_at=occurred, urgent=urgent, unread=unread,
                                  version=3, status=status))
        session.add(MunicipalEvent(id='event-1', source=payload['source'], event_id=payload['event_id'],
                                   call_id='call-1', occurred_at=occurred,
                                   event_type=payload['event_type'], data=payload))
        session.commit()


def test_reviewed_handoff_is_atomic_and_repeatable(setup):
    client, engine = setup
    add_call(engine)
    hospital = {'Authorization': 'Bearer hospital-test'}
    staff = {'Authorization': 'Bearer municipal-test'}
    assert client.get('/api/team/consultation-results', headers=hospital).json() == []
    response = client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3})
    assert response.status_code == 200
    rows = client.get('/api/team/consultation-results', headers=hospital).json()
    assert len(rows) == 1 and rows[0]['event_id'] == 'turn:turn-1'
    retry = client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3})
    assert retry.status_code == 200 and retry.json()['duplicate'] is True
    assert len(client.get('/api/team/consultation-results', headers=hospital).json()) == 1


def test_handoff_requires_review_current_version_and_staff_auth(setup):
    client, engine = setup
    add_call(engine, status='received', unread=True)
    path = '/api/municipality/inbox/call-1/handoff'
    assert client.post(path, json={'expected_version': 3}).status_code == 401
    assert client.post(path, headers={'Authorization': 'Bearer hospital-test'}, json={'expected_version': 3}).status_code == 401
    assert client.post(path, headers={'Authorization': 'Bearer municipal-test'}, json={'expected_version': 3}).status_code == 409
    assert client.get('/api/team/consultation-results', headers={'Authorization': 'Bearer hospital-test'}).json() == []


def test_urgent_handoff_preserves_unrecorded_utterance_as_such(setup):
    client, engine = setup
    add_call(engine, urgent=True, user_text='')
    response = client.post('/api/municipality/inbox/call-1/handoff',
                           headers={'Authorization': 'Bearer municipal-test'}, json={'expected_version': 3})
    assert response.status_code == 200
    rows = client.get('/api/team/consultation-results', headers={'Authorization': 'Bearer hospital-test'}).json()
    assert rows[0]['answer_status'] == 'emergency'
    assert rows[0]['user_text'] == '긴급 발화 내용 미기록'


def test_stale_review_never_hands_over_newer_event(setup):
    client, engine = setup
    add_call(engine)
    with Session(engine) as session:
        case = session.get(MunicipalCase, 'call-1')
        case.version += 1
        case.unread = True
        case.status = 'received'
        session.add(case)
        session.commit()
    response = client.post('/api/municipality/inbox/call-1/handoff',
                           headers={'Authorization': 'Bearer municipal-test'}, json={'expected_version': 3})
    assert response.status_code == 409
    assert client.get('/api/team/consultation-results', headers={'Authorization': 'Bearer hospital-test'}).json() == []


def test_conflicting_hospital_record_rolls_back_entire_handoff(setup):
    client, engine = setup
    add_call(engine)
    with Session(engine) as session:
        session.add(ConsultationRecord(id='existing', source='hospital-ai', event_id='turn:turn-1',
                                       occurred_at='2026-01-01T00:00:00+00:00',
                                       data={'source': 'conflicting'}))
        session.commit()
    response = client.post('/api/municipality/inbox/call-1/handoff',
                           headers={'Authorization': 'Bearer municipal-test'}, json={'expected_version': 3})
    assert response.status_code == 409
    with Session(engine) as session:
        case = session.get(MunicipalCase, 'call-1')
        assert case.status == 'acknowledged' and case.version == 3


def test_hospital_handoffs_exclude_direct_records_and_require_hospital_token(setup):
    client, engine = setup
    add_call(engine)
    with Session(engine) as session:
        session.add(ConsultationRecord(id='old-direct', source='legacy', event_id='old-direct',
                                       occurred_at='2026-01-01T00:00:00+00:00',
                                       data={'source': 'legacy', 'event_id': 'old-direct',
                                             'call_id': 'call-1', 'assistant_text': '이전 직접 전송'}))
        session.commit()
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    assert client.get('/api/team/municipality-handoffs').status_code == 401
    assert client.get('/api/team/municipality-handoffs', headers=staff).status_code == 401
    assert client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs'] == []
    response = client.post('/api/municipality/inbox/call-1/handoff', headers=staff,
                           json={'expected_version': 3})
    assert response.status_code == 200
    body = client.get('/api/team/municipality-handoffs', headers=hospital).json()
    assert len(body['handoffs']) == 1
    assert body['handoffs'][0]['handoff_id'] == response.json()['handoff_id']
    assert [event['event_id'] for event in body['handoffs'][0]['events']] == ['turn:turn-1']
    client.post('/api/municipality/inbox/call-1/handoff', headers=staff, json={'expected_version': 3})
    assert len(client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs']) == 1


def test_subsequent_handoff_shows_each_approved_event_once(setup):
    client, engine = setup
    add_call(engine)
    staff = {'Authorization': 'Bearer municipal-test'}
    hospital = {'Authorization': 'Bearer hospital-test'}
    path = '/api/municipality/inbox/call-1/handoff'
    assert client.post(path, headers=staff, json={'expected_version': 3}).status_code == 200
    with Session(engine) as session:
        row = session.get(MunicipalCase, 'call-1')
        row.version = 5
        row.status = 'acknowledged'
        row.unread = False
        original = session.get(MunicipalEvent, 'event-1')
        data = {**original.data, 'event_id': 'turn:turn-2', 'user_text': '증상이 계속됩니다.'}
        session.add(row)
        session.add(MunicipalEvent(id='event-2', source='hospital-ai', event_id='turn:turn-2',
                                   call_id='call-1', occurred_at=data['occurred_at'],
                                   event_type='turn_completed', data=data))
        session.commit()
    assert client.post(path, headers=staff, json={'expected_version': 5}).status_code == 200
    handoffs = client.get('/api/team/municipality-handoffs', headers=hospital).json()['handoffs']
    identifiers = [entry['event_id'] for handoff in handoffs for entry in handoff['events']]
    assert sorted(identifiers) == ['turn:turn-1', 'turn:turn-2']
