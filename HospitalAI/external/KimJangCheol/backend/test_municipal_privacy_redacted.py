"""Location-redacted events remain distinct from interrupted answers."""

from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'test-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'test-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'test-hospital')
    with TestClient(create_app(tmp_path / 'redacted.sqlite')) as instance:
        yield instance


def event():
    digest = sha256(b'call-1:utterance-1').hexdigest()
    return {'source': 'hospital-ai', 'call_id': 'call-1', 'event_id': f'redacted:{digest}',
            'event_type': 'privacy_redacted', 'phone': '01000000000',
            'occurred_at': '2026-10-31T00:00:00Z',
            'user_text': '위치 정보가 포함될 수 있어 원문 확인 제한', 'assistant_text': '',
            'answer_status': None, 'sources': []}


def test_privacy_redacted_event_is_idempotent_and_not_interrupted(client):
    sender, staff = {'Authorization': 'Bearer test-intake'}, {'Authorization': 'Bearer test-staff'}
    item = event()
    assert client.post('/api/municipality/intake', json=item, headers=sender).status_code == 201
    assert client.post('/api/municipality/intake', json=item, headers=sender).status_code == 200
    assert client.post('/api/municipality/intake', json={**item, 'occurred_at': '2026-10-31T00:00:01Z'}, headers=sender).status_code == 409
    saved = client.get('/api/municipality/inbox/call-1', headers=staff).json()['events'][0]
    assert saved['event_type'] == 'privacy_redacted'
    assert saved['answer_status'] is None and saved['assistant_text'] == ''
    assert saved['user_text'] == item['user_text']
    capabilities = client.get('/api/municipality/intake/capabilities', headers=sender).json()
    assert capabilities['privacy_redacted'] is True


@pytest.mark.parametrize('change', [
    {'event_id': 'redacted:bad'}, {'user_text': '화순 동면'}, {'assistant_text': '의료 답변'},
    {'answer_status': 'interrupted'}, {'utterance_id': 'u-1'},
    {'sources': [{'id': 'x', 'type': 'clinic', 'source': 'x'}]},
    {'recommendation_event_ids': ['private-id']},
])
def test_privacy_redacted_rejects_raw_data_and_invalid_identity(client, change):
    result = client.post('/api/municipality/intake', json={**event(), **change},
                         headers={'Authorization': 'Bearer test-intake'})
    assert result.status_code == 422
