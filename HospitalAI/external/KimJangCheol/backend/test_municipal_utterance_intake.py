"""Intake of spoken utterances before their answers are ready."""

from hashlib import sha256
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.mark.parametrize('first', ['utterance', 'turn'])
def test_raw_utterance_and_answer_arrive_once_in_either_order(tmp_path, monkeypatch, first):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'utterance.sqlite')
    sender = {'Authorization': 'Bearer test-intake'}
    staff = {'Authorization': 'Bearer test-staff'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': 'Patient words',
            'assistant_text': '', 'answer_status': None, 'sources': [], 'utterance_id': 'u-1'}
    digest = sha256(b'call-1:u-1').hexdigest()
    utterance = {**base, 'event_type': 'caller_utterance', 'event_id': f'utterance:{digest}'}
    turn = {**base, 'event_type': 'turn_completed', 'event_id': 'turn:1',
            'assistant_text': 'An answer', 'answer_status': 'completed'}
    ordered = [utterance, turn] if first == 'utterance' else [turn, utterance]
    with TestClient(app) as client:
        for item in ordered:
            assert client.post('/api/municipality/intake', json=item, headers=sender).status_code == 201
            assert client.post('/api/municipality/intake', json=item, headers=sender).status_code == 200
        detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert {event['event_type'] for event in detail['events']} == {'caller_utterance', 'turn_completed'}
        conflict = {**turn, 'event_id': 'turn:other', 'user_text': 'Different words'}
        assert client.post('/api/municipality/intake', json=conflict, headers=sender).status_code == 409
        missing = {**utterance, 'event_id': 'other'}
        missing.pop('utterance_id')
        assert client.post('/api/municipality/intake', json=missing, headers=sender).status_code == 422


def test_late_utterance_does_not_reopen_already_reviewed_answer(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'late-utterance.sqlite')
    sender, staff = {'Authorization': 'Bearer test-intake'}, {'Authorization': 'Bearer test-staff'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': 'Patient words',
            'assistant_text': 'An answer', 'answer_status': 'completed', 'sources': [], 'utterance_id': 'u-1'}
    turn = {**base, 'event_type': 'turn_completed', 'event_id': 'turn:1'}
    digest = sha256(b'call-1:u-1').hexdigest()
    utterance = {**base, 'event_type': 'caller_utterance', 'event_id': f'utterance:{digest}',
                 'assistant_text': '', 'answer_status': None}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=turn, headers=sender).status_code == 201
        assert client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=staff).status_code == 200
        assert client.post('/api/municipality/intake', json=utterance, headers=sender).status_code == 201
        detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert not detail['unread'] and detail['status'] == 'acknowledged'


def test_capability_discovery_requires_intake_credential(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'capability.sqlite')
    with TestClient(app) as client:
        assert client.get('/api/municipality/intake/capabilities').status_code == 401
        assert client.get('/api/municipality/intake/capabilities', headers={'Authorization': 'Bearer test-staff'}).status_code == 401
        result = client.get('/api/municipality/intake/capabilities', headers={'Authorization': 'Bearer test-intake'})
        assert result.status_code == 200 and result.json()['caller_utterance'] is True
        assert result.json()['location_tombstones'] is True


def test_legacy_event_retry_omitting_new_optional_fields_is_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    path = tmp_path / 'legacy.sqlite'
    app = create_app(path)
    sender = {'Authorization': 'Bearer test-intake'}
    body = {'source': 'hospital-ai', 'call_id': 'call-1', 'event_id': 'turn:old',
            'event_type': 'turn_completed', 'phone': '01000000000', 'occurred_at': '2026-10-31T00:00:00Z',
            'user_text': 'Patient words', 'assistant_text': 'An answer', 'answer_status': 'completed', 'sources': []}
    with TestClient(app) as client:
        assert client.post('/api/municipality/intake', json=body, headers=sender).status_code == 201
        with sqlite3.connect(path) as connection:
            stored = json.loads(connection.execute('SELECT data FROM municipal_events').fetchone()[0])
            for field in ('utterance_id', 'user_location_revision', 'end_reason', 'location_generation', 'location_generations',
                          'recommendation_event_ids', 'recommendation'):
                stored.pop(field, None)
            connection.execute('UPDATE municipal_events SET data=?', (json.dumps(stored),))
        retry = client.post('/api/municipality/intake', json=body, headers=sender)
        assert retry.status_code == 200 and retry.json()['duplicate'] is True


def test_equal_timestamp_events_display_in_call_lifecycle_order(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    app = create_app(tmp_path / 'same-time.sqlite')
    sender = {'Authorization': 'Bearer test-intake'}
    staff = {'Authorization': 'Bearer test-staff'}
    base = {'source': 'hospital-ai', 'call_id': 'call-1', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z', 'user_text': '', 'assistant_text': '',
            'answer_status': None, 'sources': []}
    digest = sha256(b'call-1:u-1').hexdigest()
    events = [
        {**base, 'event_type': 'call_ended', 'event_id': 'end:call-1', 'end_reason': 'caller'},
        {**base, 'event_type': 'location_revoked', 'event_id': 'revoke:1', 'location_generation': 1},
        {**base, 'event_type': 'playback_completed', 'event_id': 'playback:turn-1'},
        {**base, 'event_type': 'recommendation', 'event_id': 'recommendation:2', 'location_generation': 2,
         'recommendation': {'specialty': None, 'hospitals': [], 'warning': None}},
        {**base, 'event_type': 'turn_completed', 'event_id': 'turn:z', 'utterance_id': 'u-1',
         'user_text': 'Patient words', 'assistant_text': 'Answer', 'answer_status': 'completed'},
        {**base, 'event_type': 'turn_completed', 'event_id': 'turn:a', 'user_text': 'More words',
         'assistant_text': 'Answer', 'answer_status': 'completed'},
        {**base, 'event_type': 'caller_utterance', 'event_id': f'utterance:{digest}', 'utterance_id': 'u-1',
         'user_text': 'Patient words'},
        {**base, 'event_type': 'call_started', 'event_id': 'start:call-1'},
    ]
    with TestClient(app) as client:
        for event in events:
            assert client.post('/api/municipality/intake', json=event, headers=sender).status_code == 201
        detail = client.get('/api/municipality/inbox/call-1', headers=staff).json()
        assert [event['event_id'] for event in detail['events']] == [
            'start:call-1', f'utterance:{digest}', 'turn:z', 'turn:a', 'recommendation:2',
            'revoke:1', 'playback:turn-1', 'end:call-1']
