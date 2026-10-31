"""Consent-gated telephone consultation API scenarios."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS, TTSResult


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "calls.sqlite"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as test_client:
        yield test_client


def new_call(client: TestClient) -> str:
    response = client.post("/calls", json={"phone": "+821000000001"})
    assert response.status_code in (200, 201)
    return response.json()["id"]


def test_inbound_session_starts_in_consent_state(client: TestClient) -> None:
    response = client.post("/calls", json={"phone": "+821000000001"})
    assert response.status_code in (200, 201)
    result = response.json()
    assert result["service_number"] == "070-1122-3344"
    assert result["state"] == "awaiting_consent"
    assert result["prompt"]


def test_call_access_requires_service_authentication(client: TestClient) -> None:
    result = client.post("/calls", json={"phone": "+821000000001"},
                         headers={"Authorization": "Bearer invalid"})
    assert result.status_code in (401, 403)


@pytest.mark.parametrize("text", ["동의해도 되나요?", "동의 안 하는 건가요?", "잘 모르겠습니다", "동의?", "동의？", "동의하지만 위치는 싫어요"])
def test_ambiguous_consent_does_not_unlock_consultation(client: TestClient, text: str) -> None:
    call_id = new_call(client)
    response = client.post(f"/calls/{call_id}/consent", json={"text": text})
    assert response.status_code == 200
    assert response.json()["state"] != "accepted"
    blocked = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "기침이 있어요"})
    assert blocked.status_code == 403


@pytest.mark.parametrize("text", ["동의하지 않습니다", "동의 안합니다", "동의 안 해요"])
def test_refusal_is_never_recorded_as_consent(client: TestClient, text: str) -> None:
    call_id = new_call(client)
    response = client.post(f"/calls/{call_id}/consent", json={"text": text})
    assert response.status_code == 200
    assert response.json()["state"] != "accepted"
    blocked = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "기침이 있어요"})
    assert blocked.status_code == 403


def test_consultation_requires_consent_on_new_and_existing_turn_routes(client: TestClient) -> None:
    call_id = new_call(client)
    payload = {"request_id": "a", "text": "기침이 있어요"}
    new_route = client.post(f"/calls/{call_id}/messages", json=payload)
    legacy_route = client.post(f"/sessions/{call_id}/turns", json=payload)
    assert new_route.status_code == 403
    assert legacy_route.status_code == 403


def test_accepted_call_cannot_bypass_outbox_via_legacy_turn_route(client: TestClient) -> None:
    call_id = new_call(client)
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    response = client.post(f"/sessions/{call_id}/turns", json={"request_id": "a", "text": "기침이 있어요"})
    assert response.status_code == 403
    assert client.get(f"/sessions/{call_id}/history").json() == []


def test_positive_consent_allows_recorded_consultation(client: TestClient) -> None:
    call_id = new_call(client)
    consent = client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    assert consent.status_code == 200
    assert consent.json()["state"] == "accepted"
    response = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "기침이 있어요"})
    assert response.status_code == 202
    stored = client.get(f"/calls/{call_id}/messages/a")
    assert stored.status_code == 200
    assert stored.json()["status"] == "completed"
    assert stored.json()["final_text"]


def test_simple_affirmative_answers_one_consent_question(client: TestClient) -> None:
    call_id = new_call(client)
    assert client.post(f"/calls/{call_id}/consent", json={"text": "네"}).json()["state"] == "accepted"


def test_demo_response_does_not_repeat_unreviewed_medical_sources(client: TestClient) -> None:
    call_id = new_call(client)
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    client.post(f"/calls/{call_id}/messages", json={"request_id": "demo", "text": "기침이 있어요"})
    turn = client.get(f"/calls/{call_id}/messages/demo").json()
    assert turn["status"] == "completed"
    sources = json.loads(turn["sources_json"])
    assert sources
    assert all(source["text"] not in turn["final_text"] for source in sources)


def test_call_turn_idempotency_does_not_duplicate_consultations(client: TestClient) -> None:
    call_id = new_call(client)
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    payload = {"request_id": "a", "text": "기침이 있어요"}
    first = client.post(f"/calls/{call_id}/messages", json=payload)
    second = client.post(f"/calls/{call_id}/messages", json=payload)
    conflict = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "다른 내용"})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert conflict.status_code == 409


def test_other_call_cannot_retrieve_consultation_turn(client: TestClient) -> None:
    first = new_call(client)
    second = new_call(client)
    client.post(f"/calls/{first}/consent", json={"text": "동의"})
    client.post(f"/calls/{first}/messages", json={"request_id": "a", "text": "기침이 있어요"})
    assert client.get(f"/calls/{second}/messages/a").status_code == 404


def test_refusal_cannot_be_changed_to_accepted_after_call_is_closed(client: TestClient) -> None:
    call_id = new_call(client)
    declined = client.post(f"/calls/{call_id}/consent", json={"text": "동의하지 않습니다"})
    assert declined.json()["state"] == "declined"
    retry = client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    assert retry.status_code == 409


def test_blocked_consultation_text_is_not_stored(client: TestClient) -> None:
    call_id = new_call(client)
    response = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "개인 상담 내용"})
    assert response.status_code == 403
    history = client.get(f"/sessions/{call_id}/history")
    assert history.status_code == 200
    assert history.json() == []


def test_ready_voice_is_unavailable_until_consent(client: TestClient) -> None:
    call_id = new_call(client)
    response = client.get(f"/calls/{call_id}/prompts/ready/audio")
    assert response.status_code == 403


def test_mock_provider_does_not_claim_prompt_audio_exists(client: TestClient) -> None:
    call_id = new_call(client)
    response = client.get(f"/calls/{call_id}/prompts/consent/audio")
    assert response.status_code == 503


def test_pesticide_exposure_with_dizziness_receives_urgent_handling(client: TestClient) -> None:
    call_id = new_call(client)
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    turn = client.post(f"/calls/{call_id}/messages", json={"request_id": "a", "text": "어제 농약을 뿌렸는데 어지러워요"})
    assert turn.status_code == 202
    assert turn.json()["status"] == "emergency"
    assert turn.json()["urgent"] is True
    assert client.get(f"/calls/{call_id}/messages/a").status_code == 409


def test_emergency_before_consent_offers_local_safety_without_saving_question(client: TestClient) -> None:
    call_id = new_call(client)
    result = client.post(f"/calls/{call_id}/consent", json={"text": "농약을 어제 뿌렸는데 어질어질해요"})
    assert result.json()["state"] == "awaiting_consent"
    assert result.json()["urgent"] is True
    assert client.get(f"/sessions/{call_id}/history").json() == []


def test_preconsent_urgent_audio_uses_same_text_as_urgent_response(tmp_path: Path) -> None:
    class RecordingTTS:
        def __init__(self) -> None:
            self.spoken = ""

        async def deliver(self, text: str, kind: str) -> TTSResult:
            self.spoken = text
            return TTSResult("synthesized", kind, text, False, b"demo", "audio/mpeg")

    path = tmp_path / "urgent.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    tts = RecordingTTS()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), tts))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = new_call(client)
        reply = client.post(f"/calls/{call_id}/consent", json={"text": "농약 때문에 어지러워요"}).json()
        audio = client.get(f"/calls/{call_id}/prompts/urgent/audio")
        assert audio.status_code == 200
        assert reply["prompt"] == tts.spoken
        assert client.get(f"/sessions/{call_id}/history").json() == []
