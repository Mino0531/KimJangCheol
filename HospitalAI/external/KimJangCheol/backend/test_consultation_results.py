from pathlib import Path
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'hospital-test-token')
    with TestClient(create_app(tmp_path / 'hospital.sqlite3')) as value:
        yield value


@pytest.fixture
def example() -> dict:
    return {
        'source': 'hospital-ai', 'event_id': 'synthetic-event-1', 'call_id': 'synthetic-call-1',
        'occurred_at': '2026-10-30T06:00:00Z', 'phone': '+821000000001',
        'user_text': '가상 상담 질문', 'assistant_text': '가상 답변',
        'answer_status': 'completed', 'sources': [{'id': 'test-1', 'type': 'clinic', 'source': 'demo'}],
    }


def test_results_require_authentication(client: TestClient, example: dict) -> None:
    assert client.post('/api/team/consultation-results', json=example).status_code == 401
    assert client.get('/api/team/consultation-results').status_code == 401


def test_results_are_idempotent_and_isolated_from_public_legacy_routes(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    assert client.post('/api/team/consultation-results', json=example, headers=headers).status_code == 201
    duplicate = client.post('/api/team/consultation-results', json=example, headers=headers)
    assert duplicate.status_code == 200 and duplicate.json()['duplicate'] is True
    assert client.post('/api/team/consultation-results', json={**example, 'user_text': '다른 질문'}, headers=headers).status_code == 409
    response = client.get('/api/team/consultation-results', headers=headers)
    assert response.headers['cache-control'] == 'no-store'
    results = response.json()
    assert len(results) == 1 and results[0]['assistant_text'] == example['assistant_text']
    assert example['call_id'] not in client.get('/api/team/bootstrap').text


def test_results_require_timezone_and_declared_fields(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    assert client.post('/api/team/consultation-results', json={**example, 'occurred_at': '2026-10-30T06:00:00'}, headers=headers).status_code == 422
    assert client.post('/api/team/consultation-results', json={**example, 'unexpected': 'test'}, headers=headers).status_code == 422


def test_historical_structured_answer_is_redacted_on_read_without_changing_stored_event(
    client: TestClient, example: dict, tmp_path: Path,
) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = json.dumps({'display_text': '화면 표시 문장', 'tts_text': '음성 전용 문장', 'internal': 'internal-secret'}, ensure_ascii=False)
    event = {**example, 'assistant_text': raw}
    assert client.post('/api/team/consultation-results', json=event, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert result['assistant_text'] == '화면 표시 문장'
    assert 'internal-secret' not in str(result)
    with sqlite3.connect(tmp_path / 'hospital.sqlite3') as db:
        stored = json.loads(db.execute('SELECT data FROM consultation_results').fetchone()[0])
    assert stored['assistant_text'] == raw
    assert client.post('/api/team/consultation-results', json=event, headers=headers).json()['duplicate'] is True


def test_malformed_json_does_not_reach_dashboard(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    event = {**example, 'assistant_text': '{"display_text":"internal-secret"'}
    assert client.post('/api/team/consultation-results', json=event, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert result['assistant_text'] != event['assistant_text']
    assert 'internal-secret' not in str(result)


def test_fenced_korean_json_shows_only_patient_facing_text(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = '```json\n{"표시":"화면 문장", "TTS":"음성 문장", "debug":"internal-secret"}\n```'
    assert client.post('/api/team/consultation-results', json={**example, 'assistant_text': raw}, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert result['assistant_text'] == '화면 문장'
    assert 'internal-secret' not in str(result)


def test_double_encoded_json_shows_only_display(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = json.dumps(json.dumps({'display_text': '화면 문장', 'tts_text': '음성 문장', 'internal': 'internal-secret'}, ensure_ascii=False), ensure_ascii=False)
    event = {**example, 'assistant_text': raw}
    assert client.post('/api/team/consultation-results', json=event, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert result['assistant_text'] == '화면 문장'
    assert 'internal-secret' not in str(result)
    assert client.post('/api/team/consultation-results', json=event, headers=headers).json()['duplicate'] is True


def test_double_encoded_malformed_json_is_not_displayed(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = json.dumps('{"display_text":"internal-secret"', ensure_ascii=False)
    assert client.post('/api/team/consultation-results', json={**example, 'assistant_text': raw}, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert 'internal-secret' not in result['assistant_text']


def test_ordinary_quoted_text_remains_unchanged(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = '"일반적인 안내 문장"'
    assert client.post('/api/team/consultation-results', json={**example, 'assistant_text': raw}, headers=headers).status_code == 201
    assert client.get('/api/team/consultation-results', headers=headers).json()[0]['assistant_text'] == raw


@pytest.mark.parametrize('depth', [3, 4])
def test_nested_encoded_answer_is_unwrapped(client: TestClient, example: dict, depth: int) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = json.dumps({'display_text': '표시 문장', 'tts_text': '음성 문장', 'internal': 'internal-secret'}, ensure_ascii=False)
    for _ in range(depth):
        raw = json.dumps(raw, ensure_ascii=False)
    assert client.post('/api/team/consultation-results', json={**example, 'assistant_text': raw}, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert result['assistant_text'] == '표시 문장'
    assert 'internal-secret' not in str(result)


def test_excessive_nesting_is_not_displayed(client: TestClient, example: dict) -> None:
    headers = {'Authorization': 'Bearer hospital-test-token'}
    raw = json.dumps({'display_text': '표시 문장', 'internal': 'internal-secret'}, ensure_ascii=False)
    for _ in range(5):
        raw = json.dumps(raw, ensure_ascii=False)
    assert client.post('/api/team/consultation-results', json={**example, 'assistant_text': raw}, headers=headers).status_code == 201
    result = client.get('/api/team/consultation-results', headers=headers).json()[0]
    assert 'internal-secret' not in str(result)
    assert result['assistant_text'] != raw
