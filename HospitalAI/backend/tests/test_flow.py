import json
import sqlite3
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database, Row
from app.main import create_app
from app.orchestrator import ServiceBundle, process_turn
from app.providers import build_services
from app.services import MockLLM, MockTTS, OpenAICompatibleLLM, ServiceError, SplitLLM, TTSResult


@pytest.fixture
def provider_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        'HOSPITAL_LOW_PROVIDER', 'HOSPITAL_HIGH_PROVIDER', 'HOSPITAL_ALLOW_MEDICAL_DATA',
        'GEMINI_API_KEY', 'GEMINI_MODEL', 'HOSPITAL_HIGH_BASE_URL',
        'HOSPITAL_HIGH_API_KEY', 'HOSPITAL_HIGH_MODEL',
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_default_provider_is_offline(provider_environment: pytest.MonkeyPatch) -> None:
    provider = build_services().llm
    assert isinstance(provider, SplitLLM)
    assert isinstance(provider.low_provider, MockLLM)
    assert isinstance(provider.high_provider, MockLLM)


def test_gemini_provider_uses_configured_low_model(provider_environment: pytest.MonkeyPatch) -> None:
    provider_environment.setenv('HOSPITAL_LOW_PROVIDER', 'gemini')
    provider_environment.setenv('GEMINI_API_KEY', 'dummy-test-key')
    provider_environment.setenv('GEMINI_MODEL', 'dummy-low-model')
    provider = build_services().llm
    assert isinstance(provider, SplitLLM)
    assert isinstance(provider.low_provider, OpenAICompatibleLLM)
    assert provider.low_provider.config.low_model == 'dummy-low-model'
    assert provider.low_provider.config.base_url == 'https://generativelanguage.googleapis.com/v1beta/openai/'
    assert provider.low_provider.config.api_key == 'dummy-test-key'
    assert isinstance(provider.high_provider, MockLLM)


def test_external_reasoning_requires_explicit_authorization(provider_environment: pytest.MonkeyPatch) -> None:
    provider_environment.setenv('HOSPITAL_HIGH_PROVIDER', 'openai-compatible')
    with pytest.raises(ServiceError):
        build_services()


def test_external_reasoning_accepts_separate_provider_configuration(provider_environment: pytest.MonkeyPatch) -> None:
    provider_environment.setenv('HOSPITAL_HIGH_PROVIDER', 'openai-compatible')
    provider_environment.setenv('HOSPITAL_ALLOW_MEDICAL_DATA', 'true')
    provider_environment.setenv('HOSPITAL_HIGH_BASE_URL', 'https://example.com/v1/')
    provider_environment.setenv('HOSPITAL_HIGH_API_KEY', 'dummy-high-key')
    provider_environment.setenv('HOSPITAL_HIGH_MODEL', 'dummy-high-model')
    provider = build_services().llm
    assert isinstance(provider, SplitLLM)
    assert isinstance(provider.low_provider, MockLLM)
    assert isinstance(provider.high_provider, OpenAICompatibleLLM)
    assert provider.high_provider.config.reasoning_model == 'dummy-high-model'
    assert provider.high_provider.config.base_url == 'https://example.com/v1/'
    assert provider.high_provider.config.allow_medical_data


@pytest.fixture
def client(tmp_path: Path):
    path = tmp_path / 'test.sqlite'
    settings = Settings(
        environment='development',
        service_token='service',
        admin_token='admin',
        dev_verification_token='verify',
        seed_demo=True,
        database_path=str(path),
    )
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as test_client:
        yield test_client


def create_session(client: TestClient, phone: str, verified: bool = True) -> str:
    payload = {'phone': phone}
    if verified:
        payload['verification_token'] = 'verify'
    response = client.post('/sessions', json=payload)
    assert response.status_code in (200, 201)
    return response.json()['id']


def test_api_requires_service_authentication(client: TestClient) -> None:
    response = client.post('/sessions', json={'phone': '+821000000001'}, headers={'Authorization': 'Bearer wrong'})
    assert response.status_code in (401, 403)


def test_development_verification_is_not_valid_in_production(tmp_path: Path) -> None:
    path = tmp_path / 'shared.sqlite'
    database = Database(path)
    services = ServiceBundle(MockLLM(), MockTTS())
    development = Settings(environment='development', service_token='service', admin_token='admin',
                           dev_verification_token='verify', seed_demo=True, database_path=str(path))
    with TestClient(create_app(development, database, services), headers={'Authorization': 'Bearer service'}) as dev:
        session_id = create_session(dev, '+821000000001')
    production = Settings(environment='production', service_token='service', admin_token='admin',
                          database_path=str(path))
    with TestClient(create_app(production, database, services), headers={'Authorization': 'Bearer service'}) as prod:
        response = prod.get(f'/sessions/{session_id}/history')
    assert response.status_code == 403


def test_unverified_phone_cannot_retrieve_patient_history(client: TestClient) -> None:
    session_id = create_session(client, '+821000000001', verified=False)
    response = client.post(f'/sessions/{session_id}/turns', json={'request_id': 'request-1', 'text': '고혈압 병력이 있나요?'})
    assert response.status_code in (200, 201, 202)
    turn = client.get(f'/sessions/{session_id}/turns/request-1')
    assert turn.status_code == 200
    assert 'sample-history-1' not in turn.text
    assert '고혈압 병력이 기록되어 있습니다' not in turn.text


def test_verified_patient_search_is_scoped_to_that_patient(client: TestClient) -> None:
    patient_one = create_session(client, '+821000000001')
    patient_two = create_session(client, '+821000000002')
    payload = {'request_id': 'request-1', 'text': '고혈압 병력이 있나요?'}
    client.post(f'/sessions/{patient_one}/turns', json=payload)
    client.post(f'/sessions/{patient_two}/turns', json=payload)
    first = client.get(f'/sessions/{patient_one}/turns/request-1')
    second = client.get(f'/sessions/{patient_two}/turns/request-1')
    assert 'sample-history-1' in first.text
    assert 'sample-history-1' not in second.text


def test_duplicate_request_is_idempotent_and_conflicting_input_rejected(client: TestClient) -> None:
    session_id = create_session(client, '+821000000001')
    payload = {'request_id': 'request-1', 'text': '기침이 있어요'}
    first = client.post(f'/sessions/{session_id}/turns', json=payload)
    retry = client.post(f'/sessions/{session_id}/turns', json=payload)
    conflict = client.post(f'/sessions/{session_id}/turns', json={'request_id': 'request-1', 'text': '다른 증상'})
    assert first.status_code in (200, 201, 202)
    assert retry.status_code in (200, 201, 202)
    assert retry.json()['id'] == first.json()['id']
    assert conflict.status_code == 409
    history = client.get(f'/sessions/{session_id}/history')
    assert len(history.json()) == 1


def test_candidate_requires_admin_review_before_becoming_patient_record(client: TestClient) -> None:
    session_id = create_session(client, '+821000000001')
    turn = client.post(f'/sessions/{session_id}/turns', json={'request_id': 'request-1', 'text': '무릎이 아파요'}).json()
    candidate = client.post(f'/sessions/{session_id}/candidates', json={'turn_id': turn['id'], 'text': '무릎 통증'}).json()
    denied = client.post(f"/candidates/{candidate['id']}/review", json={'decision': 'approved', 'reviewer': 'doctor'})
    assert denied.status_code in (401, 403)
    approved = client.post(
        f"/candidates/{candidate['id']}/review",
        json={'decision': 'approved', 'reviewer': 'doctor'},
        headers={'Authorization': 'Bearer admin'},
    )
    assert approved.status_code == 200
    assert approved.json()['status'] == 'approved'


def test_both_models_start_without_waiting_for_each_other(tmp_path: Path) -> None:
    db = Database(tmp_path / 'concurrent.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '기침이 있어요')

    async def scenario() -> None:
        low_started = anyio.Event()
        high_started = anyio.Event()

        class GatedLLM:
            async def low(self, text: str) -> str:
                low_started.set()
                await high_started.wait()
                return '대기 안내'

            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                high_started.set()
                await low_started.wait()
                return '최종 응답'

        with anyio.fail_after(5):
            await process_turn(db, ServiceBundle(GatedLLM(), MockTTS()), session, turn)
        assert low_started.is_set() and high_started.is_set()

    anyio.run(scenario)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['status'] == 'completed'
    assert stored['final_text'] == '최종 응답'


def test_reasoning_completes_when_low_latency_model_fails(tmp_path: Path) -> None:
    db = Database(tmp_path / 'failure.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '기침이 있어요')

    class FailedLowLLM(MockLLM):
        async def low(self, text: str) -> str:
            raise RuntimeError('low model unavailable')

    anyio.run(process_turn, db, ServiceBundle(FailedLowLLM(), MockTTS()), session, turn)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['status'] == 'completed'
    assert stored['final_text']


def test_fast_reasoning_does_not_wait_for_a_blocked_low_model(tmp_path: Path) -> None:
    db = Database(tmp_path / 'fast-final.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '기침이 있어요')

    async def scenario() -> None:
        low_started = anyio.Event()

        class FastFinalLLM:
            async def low(self, text: str) -> str:
                low_started.set()
                await anyio.sleep_forever()

            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                await low_started.wait()
                return '최종 응답'

        with anyio.fail_after(5):
            await process_turn(db, ServiceBundle(FastFinalLLM(), MockTTS(), low_timeout=30), session, turn)

    anyio.run(scenario)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['status'] == 'completed'
    assert stored['initial_text'] is None
    assert json.loads(str(stored['tts_json'])) == {'final': 'simulated'}


def test_emergency_bypasses_models_and_prioritizes_speech(tmp_path: Path) -> None:
    db = Database(tmp_path / 'emergency.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '숨을 쉴 수 없어요')

    class ModelsMustNotRun(MockLLM):
        async def low(self, text: str) -> str:
            raise AssertionError('low model was called')

        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            raise AssertionError('reasoning model was called')

    anyio.run(process_turn, db, ServiceBundle(ModelsMustNotRun(), MockTTS()), session, turn)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['status'] == 'emergency'
    assert stored['initial_text'] is None
    assert json.loads(str(stored['tts_json'])) == {'emergency': 'simulated'}


def test_retrieval_failure_marks_the_turn_degraded(tmp_path: Path) -> None:
    class FailedSearchDatabase(Database):
        def search(self, patient_id: str | None, query: str) -> list[Row]:
            raise sqlite3.OperationalError('search unavailable')

    db = FailedSearchDatabase(tmp_path / 'search-failure.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '기침이 있어요')
    anyio.run(process_turn, db, ServiceBundle(MockLLM(), MockTTS()), session, turn)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['status'] == 'degraded'
    assert stored['error'] == 'retrieval_or_reasoning_unavailable'
    assert stored['final_text']


def test_speech_delivery_failure_does_not_erase_final_answer(tmp_path: Path) -> None:
    class FailedTTS(MockTTS):
        async def deliver(self, text: str, kind: str) -> TTSResult:
            raise RuntimeError('speech unavailable')

    db = Database(tmp_path / 'tts-failure.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'request-1', '기침이 있어요')
    anyio.run(process_turn, db, ServiceBundle(MockLLM(), FailedTTS()), session, turn)
    stored = db.get_turn(str(session['id']), 'request-1')
    assert stored is not None and stored['final_text']
    assert json.loads(str(stored['tts_json']))['final'] == 'failed'
