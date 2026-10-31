"""Behavior checks for authenticated, durable municipal call intake."""
from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select
from backend.app import create_app
from backend.municipal_intake import MunicipalEvent


def test_intake_auth_ack_and_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    event = {'source': 'hospital-ai', 'event_id': 'one', 'call_id': 'call-1',
             'occurred_at': '2026-10-30T10:00:00Z', 'event_type': 'call_started',
             'phone': '01000000000', 'user_text': '', 'assistant_text': '',
             'answer_status': None, 'sources': []}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=event).status_code == 401
        assert client.post('/api/municipality/intake', json=event, headers={'Authorization': 'Bearer staff-secret'}).status_code == 401
        sender = {'Authorization': 'Bearer intake-secret'}
        staff = {'Authorization': 'Bearer staff-secret'}
        assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        assert client.post('/api/municipality/intake', json=event, headers=sender).json()['duplicate'] is True
        assert client.get('/api/municipality/inbox', headers=sender).status_code == 401
        case = client.get('/api/municipality/inbox', headers=staff).json()['cases'][0]
        assert case['unread'] and case['version'] == 1
        assert client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff).status_code == 200
        assert client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff).status_code == 409


def test_emergency_reopens_acknowledged_call_and_rejects_conflicting_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    start = {'source': 'hospital-ai', 'event_id': 'start', 'call_id': 'call-1',
             'occurred_at': '2026-10-30T10:00:00Z', 'event_type': 'call_started',
             'phone': '01000000000', 'user_text': '', 'assistant_text': '',
             'answer_status': None, 'sources': []}
    urgent = {**start, 'event_id': 'urgent', 'event_type': 'urgent',
              'answer_status': 'emergency', 'assistant_text': '119에 연락하세요.'}
    with TestClient(app) as client:
        sender = {'Authorization': 'Bearer intake-secret'}
        staff = {'Authorization': 'Bearer staff-secret'}
        client.post('/api/municipality/intake', json=start, headers=sender)
        client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff)
        assert client.post('/api/municipality/intake', json=urgent, headers=sender).status_code == 201
        case = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert case['urgent'] and case['unread'] and case['version'] == 3
        assert client.post('/api/municipality/intake', json={**urgent, 'phone': 'different'}, headers=sender).status_code == 409


def test_equal_staff_and_intake_tokens_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'same-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'same-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    with TestClient(app) as client:
        assert client.get('/api/municipality/inbox', headers={'Authorization': 'Bearer same-secret'}).status_code == 401


def test_urgent_arrives_before_start_and_late_event_remains_visible(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    event = {'source': 'hospital-ai', 'event_id': 'emergency', 'call_id': 'call-1',
             'occurred_at': '2026-10-30T10:00:02Z', 'event_type': 'urgent',
             'phone': '01000000000', 'user_text': '', 'assistant_text': '119에 연락하세요.',
             'answer_status': 'emergency', 'sources': []}
    start = {**event, 'event_id': 'start', 'occurred_at': '2026-10-30T10:00:00Z',
             'event_type': 'call_started', 'assistant_text': '', 'answer_status': None}
    with TestClient(app) as client:
        sender = {'Authorization': 'Bearer intake-secret'}
        staff = {'Authorization': 'Bearer staff-secret'}
        assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        assert client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff).status_code == 200
        assert client.post('/api/municipality/intake', json=start, headers=sender).status_code == 201
        case = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert case['urgent'] and case['unread'] and case['status'] == 'received'
        assert [item['event_type'] for item in case['events']] == ['call_started', 'urgent']


@pytest.mark.parametrize('kind', ['playback_completed', 'call_ended'])
def test_metadata_does_not_reset_acknowledgement(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    event = {'source': 'hospital-ai', 'event_id': 'start', 'call_id': 'call-1',
             'occurred_at': '2026-10-30T10:00:00Z', 'event_type': 'call_started',
             'phone': '01000000000', 'user_text': '', 'assistant_text': '',
             'answer_status': None, 'sources': []}
    with TestClient(app) as client:
        sender = {'Authorization': 'Bearer intake-secret'}
        staff = {'Authorization': 'Bearer staff-secret'}
        assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        before = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff).json()['case']
        metadata = {**event, 'event_type': kind, 'event_id': 'playback:turn-1' if kind == 'playback_completed' else 'end:call-1'}
        if kind == 'call_ended':
            metadata['end_reason'] = 'hangup'
        assert client.post('/api/municipality/intake', json=metadata, headers=sender).status_code == 201
        after = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert (after['unread'], after['status'], after['acknowledged_at'], after['version']) == (
            before['unread'], before['status'], before['acknowledged_at'], before['version'])
        assert any(item['event_id'] == metadata['event_id'] for item in after['events'])


def test_legacy_event_replay_remains_idempotent_after_optional_field_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-secret')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-secret')
    app = create_app(tmp_path / 'municipal.sqlite')
    event = {'source': 'hospital-ai', 'event_id': 'start', 'call_id': 'call-1',
             'occurred_at': '2026-10-30T10:00:00Z', 'event_type': 'call_started',
             'phone': '01000000000', 'user_text': '', 'assistant_text': '',
             'answer_status': None, 'sources': []}
    sender = {'Authorization': 'Bearer intake-secret'}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        with Session(app.state.engine) as session:
            stored = session.exec(select(MunicipalEvent)).one()
            stored.data = {key: value for key, value in stored.data.items() if key != 'end_reason'}
            session.add(stored)
            session.commit()
        retry = client.post('/api/municipality/intake', json=event, headers=sender)
        assert retry.status_code == 200 and retry.json()['duplicate'] is True
