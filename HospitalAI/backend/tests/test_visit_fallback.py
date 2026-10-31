from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def test_missing_knowledge_preserves_failure_and_offers_non_diagnostic_visit_help(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / 'visit.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    monkeypatch.setattr('app.call_search.CallDatabase.search', lambda _self, _patient_id, _query: [])
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/messages', json={'request_id': 'first', 'text': '무릎이 아파요'})
        result = client.get(f'/calls/{call_id}/messages/first').json()
    assert result['status'] == 'degraded'
    assert result['error'] == 'grounding_unavailable'
    assert result['guidance']['action'] == 'hospital_search_offer'


def test_provider_failure_preserves_failure_and_offers_non_diagnostic_visit_help(tmp_path: Path, monkeypatch) -> None:
    class BrokenLLM:
        async def low(self, text: str) -> str:
            return '잠시만요.'

        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            raise OSError('unavailable')

    database = Database(tmp_path / 'provider.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(BrokenLLM(), MockTTS()))
    monkeypatch.setattr('app.call_search.CallDatabase.search', lambda _self, _patient_id, _query: [{'id': 'clinic:1', 'text': 'test', 'type': 'clinic', 'source': 'test'}])
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/messages', json={'request_id': 'first', 'text': '무릎이 아파요'})
        result = client.get(f'/calls/{call_id}/messages/first').json()
    assert result['status'] == 'degraded'
    assert result['error'] == 'retrieval_or_reasoning_unavailable'
    assert result['guidance']['action'] == 'hospital_search_offer'


def test_unknown_department_offers_general_hospital_search(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / 'unknown.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    monkeypatch.setattr('app.call_search.CallDatabase.search', lambda _self, _patient_id, _query: [])
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/messages', json={'request_id': 'first', 'text': '속이 불편해요'})
        result = client.get(f'/calls/{call_id}/messages/first').json()
    assert result['status'] == 'degraded'
    assert result['guidance']['action'] == 'hospital_search_offer'
    assert result['guidance']['specialty'] is None


def test_repeated_missing_knowledge_preserves_active_call_and_new_turn(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / 'followup.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    monkeypatch.setattr('app.call_search.CallDatabase.search', lambda _self, _patient_id, _query: [])
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        for request_id, text in [('first', '무릎이 아파요'), ('followup', '준비물은 챙겼어요')]:
            response = client.post(f'/calls/{call_id}/messages', json={'request_id': request_id, 'text': text})
            assert response.status_code == 202
            result = client.get(f'/calls/{call_id}/messages/{request_id}').json()
            assert result['status'] == 'degraded'
            assert result['error'] == 'grounding_unavailable'
        assert client.get(f'/calls/{call_id}').json()['state'] == 'accepted'
    assert len(database.history(call_id)) == 2
