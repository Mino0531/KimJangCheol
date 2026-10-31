"""Interrupted calls retain caller speech through authenticated staff handoff."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'intake-test')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'staff-test')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'hospital-test')
    with TestClient(create_app(tmp_path / 'interrupted.sqlite')) as test_client:
        yield test_client


def interrupted_event() -> dict:
    return {
        'source': 'hospital-ai', 'event_id': 'interrupted:turn-1', 'call_id': 'call-1',
        'occurred_at': '2026-10-30T10:00:00Z', 'event_type': 'turn_interrupted',
        'phone': '01000000000', 'user_text': '며칠 전부터 무릎이 아파요.',
        'assistant_text': '응답이 중단됐습니다.', 'answer_status': 'interrupted', 'sources': [],
    }


SENDER = {'Authorization': 'Bearer intake-test'}
STAFF = {'Authorization': 'Bearer staff-test'}
HOSPITAL = {'Authorization': 'Bearer hospital-test'}


def test_interrupted_utterance_requires_sender_auth(client: TestClient) -> None:
    assert client.post('/api/municipality/intake', json=interrupted_event()).status_code == 401
    assert client.post('/api/municipality/intake', json=interrupted_event(), headers=STAFF).status_code == 401


def test_interrupted_utterance_is_received_once(client: TestClient) -> None:
    event = interrupted_event()
    first = client.post('/api/municipality/intake', json=event, headers=SENDER)
    assert first.status_code == 201
    second = client.post('/api/municipality/intake', json=event, headers=SENDER)
    assert second.status_code == 200 and second.json()['duplicate'] is True
    items = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()['events']
    assert len(items) == 1
    assert items[0]['event_type'] == 'turn_interrupted'
    assert items[0]['answer_status'] == 'interrupted'
    assert items[0]['user_text'] == event['user_text']
    assert items[0]['assistant_text'] == event['assistant_text']


def test_interrupted_utterance_must_not_be_empty(client: TestClient) -> None:
    event = {**interrupted_event(), 'user_text': '   '}
    assert client.post('/api/municipality/intake', json=event, headers=SENDER).status_code == 422


@pytest.mark.parametrize('changes', [
    {'assistant_text': '완료된 안내입니다.'},
    {'answer_status': 'completed'},
    {'sources': [{'id': 'clinic:1', 'type': 'clinic', 'source': 'reference'}]},
])
def test_interrupted_event_rejects_answer_or_source_claims(client: TestClient, changes: dict) -> None:
    event = {**interrupted_event(), **changes}
    assert client.post('/api/municipality/intake', json=event, headers=SENDER).status_code == 422


def test_interrupted_utterance_reaches_hospital_only_after_review(client: TestClient) -> None:
    event = interrupted_event()
    assert client.post('/api/municipality/intake', json=event, headers=SENDER).status_code == 201
    assert client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs'] == []
    assert client.post('/api/municipality/inbox/call-1/handoff', json={'expected_version': 1}, headers=STAFF).status_code == 409
    acknowledged = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=STAFF)
    assert acknowledged.status_code == 200
    version = acknowledged.json()['case']['version']
    assert client.post('/api/municipality/inbox/call-1/handoff', json={'expected_version': version}, headers=STAFF).status_code == 200
    rows = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert len(rows) == 1
    assert rows[0]['events'][0]['user_text'] == event['user_text']
    assert rows[0]['events'][0]['answer_status'] == 'interrupted'
    assert rows[0]['events'][0]['assistant_text'] == event['assistant_text']


@pytest.mark.parametrize('notice', [
    '연결 오류로 응답이 중단됐습니다.',
    '통화가 종료되어 응답이 중단됐습니다.',
])
def test_interruption_reasons_survive_staff_review_and_handoff(client: TestClient, notice: str) -> None:
    event = {**interrupted_event(), 'assistant_text': notice}
    assert client.post('/api/municipality/intake', json=event, headers=SENDER).status_code == 201
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=STAFF)
    assert ack.status_code == 200
    handoff = client.post('/api/municipality/inbox/call-1/handoff',
                          json={'expected_version': ack.json()['case']['version']}, headers=STAFF)
    assert handoff.status_code == 200
    rows = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert rows[0]['events'][0]['assistant_text'] == notice


def test_ended_call_with_unanswered_utterance_reaches_hospital(client: TestClient) -> None:
    interrupted = {**interrupted_event(), 'assistant_text': '통화가 종료되어 응답이 중단됐습니다.'}
    ended = {**playback_event(), 'event_id': 'end:call-1', 'event_type': 'call_ended', 'end_reason': 'hangup'}
    assert client.post('/api/municipality/intake', json=interrupted, headers=SENDER).status_code == 201
    assert client.post('/api/municipality/intake', json=ended, headers=SENDER).status_code == 201
    case = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()
    assert any(event.get('end_reason') == 'hangup' for event in case['events'])
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': case['version']}, headers=STAFF)
    handoff = client.post('/api/municipality/inbox/call-1/handoff',
                          json={'expected_version': ack.json()['case']['version']}, headers=STAFF)
    assert handoff.status_code == 200
    rows = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert rows[0]['events'][0]['user_text'] == interrupted['user_text']
    assert rows[0]['events'][0]['assistant_text'] == interrupted['assistant_text']


def test_interrupted_event_with_prior_completed_answer_does_not_erase_history(client: TestClient) -> None:
    interrupted = interrupted_event()
    completed = {**interrupted, 'event_id': 'turn:turn-1', 'event_type': 'turn_completed',
                 'answer_status': 'completed', 'assistant_text': '의료진과 확인하세요.'}
    assert client.post('/api/municipality/intake', json=completed, headers=SENDER).status_code == 201
    assert client.post('/api/municipality/intake', json=interrupted, headers=SENDER).status_code == 201
    items = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()['events']
    assert {item['event_type'] for item in items} == {'turn_completed', 'turn_interrupted'}
    assert next(item for item in items if item['event_type'] == 'turn_interrupted')['answer_status'] == 'interrupted'
    current = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()['version']
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': current}, headers=STAFF)
    assert ack.status_code == 200
    response = client.post('/api/municipality/inbox/call-1/handoff',
                           json={'expected_version': ack.json()['case']['version']}, headers=STAFF)
    assert response.status_code == 200
    handoffs = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    statuses = {item['answer_status'] for handoff in handoffs for item in handoff['events']}
    assert statuses == {'completed', 'interrupted'}


def test_late_completed_event_after_interrupt_requires_another_review(client: TestClient) -> None:
    interrupted = interrupted_event()
    completed = {**interrupted, 'event_id': 'turn:turn-1', 'event_type': 'turn_completed',
                 'answer_status': 'completed', 'assistant_text': '의료진과 확인하세요.'}
    assert client.post('/api/municipality/intake', json=interrupted, headers=SENDER).status_code == 201
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=STAFF)
    assert ack.status_code == 200
    old_version = ack.json()['case']['version']
    assert client.post('/api/municipality/intake', json=completed, headers=SENDER).status_code == 201
    assert client.post('/api/municipality/inbox/call-1/handoff',
                       json={'expected_version': old_version}, headers=STAFF).status_code == 409
    current = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()
    assert current['unread'] is True
    fresh_ack = client.post('/api/municipality/inbox/call-1/ack',
                            json={'expected_version': current['version']}, headers=STAFF)
    assert fresh_ack.status_code == 200
    assert client.post('/api/municipality/inbox/call-1/handoff',
                       json={'expected_version': fresh_ack.json()['case']['version']}, headers=STAFF).status_code == 200
    handoffs = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    statuses = {item['answer_status'] for handoff in handoffs for item in handoff['events']}
    assert statuses == {'completed', 'interrupted'}


def playback_event() -> dict:
    return {
        'source': 'hospital-ai', 'event_id': 'playback:turn-1', 'call_id': 'call-1',
        'occurred_at': '2026-10-30T10:00:02Z', 'event_type': 'playback_completed',
        'phone': '01000000000', 'user_text': '', 'assistant_text': '',
        'answer_status': None, 'sources': [],
    }


def test_playback_receipt_is_idempotent(client: TestClient) -> None:
    event = playback_event()
    assert client.post('/api/municipality/intake', json=event, headers=SENDER).status_code == 201
    duplicate = client.post('/api/municipality/intake', json=event, headers=SENDER)
    assert duplicate.status_code == 200 and duplicate.json()['duplicate'] is True


@pytest.mark.parametrize('changes', [
    {'event_id': 'end:other-call'},
    {'end_reason': None},
    {'end_reason': 'made_up'},
    {'user_text': '상담 발화'},
    {'assistant_text': '상담 답변'},
    {'sources': [{'id': 'clinic:1', 'type': 'clinic', 'source': 'reference'}]},
])
def test_call_end_event_rejects_invalid_payload(client: TestClient, changes: dict) -> None:
    event = {**playback_event(), 'event_id': 'end:call-1', 'event_type': 'call_ended', 'end_reason': 'caller'}
    assert client.post('/api/municipality/intake', json={**event, **changes}, headers=SENDER).status_code == 422


@pytest.mark.parametrize('changes', [
    {'user_text': '새로운 상담 내용'},
    {'assistant_text': '새로운 응답'},
    {'answer_status': 'completed'},
    {'sources': [{'id': 'clinic:1', 'type': 'clinic', 'source': 'reference'}]},
    {'event_id': 'turn:turn-1'},
])
def test_playback_receipt_rejects_forged_consultation_content(client: TestClient, changes: dict) -> None:
    assert client.post('/api/municipality/intake', json={**playback_event(), **changes}, headers=SENDER).status_code == 422


def test_playback_receipt_does_not_reopen_handed_over_case(client: TestClient) -> None:
    completed = {**interrupted_event(), 'event_id': 'turn:turn-1', 'event_type': 'turn_completed',
                 'answer_status': 'completed', 'assistant_text': '의료진과 확인하세요.'}
    assert client.post('/api/municipality/intake', json=completed, headers=SENDER).status_code == 201
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': 1}, headers=STAFF)
    assert ack.status_code == 200
    version = ack.json()['case']['version']
    assert client.post('/api/municipality/inbox/call-1/handoff', json={'expected_version': version}, headers=STAFF).status_code == 200
    playback = playback_event()
    assert client.post('/api/municipality/intake', json=playback, headers=SENDER).status_code == 201
    previous = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert all(playback['event_id'] not in handoff.get('playback_completed_event_ids', []) for handoff in previous)
    current = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()
    assert current['unread'] is False
    assert current['status'] == 'handed_over'
    prior_retry = client.post('/api/municipality/inbox/call-1/handoff',
                              json={'expected_version': version}, headers=STAFF)
    assert prior_retry.status_code == 200 and prior_retry.json()['duplicate'] is True
    after_retry = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert all(playback['event_id'] not in handoff.get('playback_completed_event_ids', []) for handoff in after_retry)
    assert any(item['event_type'] == 'playback_completed' for item in current['events'])


def test_playback_before_answer_is_preserved_for_later_review(client: TestClient) -> None:
    playback = playback_event()
    completed = {**interrupted_event(), 'event_id': 'turn:turn-1', 'event_type': 'turn_completed',
                 'answer_status': 'completed', 'assistant_text': '의료진과 확인하세요.'}
    assert client.post('/api/municipality/intake', json=playback, headers=SENDER).status_code == 201
    assert client.post('/api/municipality/intake', json=completed, headers=SENDER).status_code == 201
    current = client.get('/api/municipality/inbox/call-1', headers=STAFF).json()
    ack = client.post('/api/municipality/inbox/call-1/ack', json={'expected_version': current['version']}, headers=STAFF)
    assert ack.status_code == 200
    assert client.post('/api/municipality/inbox/call-1/handoff',
                       json={'expected_version': ack.json()['case']['version']}, headers=STAFF).status_code == 200
    rows = client.get('/api/team/municipality-handoffs', headers=HOSPITAL).json()['handoffs']
    assert any(playback['event_id'] in handoff.get('playback_completed_event_ids', []) for handoff in rows)
