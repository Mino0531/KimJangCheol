"""Location-first voice prompts stay behind the call-consent boundary."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, PESTICIDE_EMERGENCY_MESSAGE, TTSResult


class RecordingSpeech:
    def __init__(self) -> None:
        self.last_text = ""

    async def deliver(self, text: str, kind: str) -> TTSResult:
        self.last_text = text
        return TTSResult("synthesized", kind, text, False, b"demo", "audio/mpeg")


def test_question_prompt_requires_call_consent(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call = client.post("/calls", json={"phone": "+821000000001"}).json()
        response = client.get(f"/calls/{call['id']}/prompts/question/audio")
        assert response.status_code == 403
        assert speech.last_text == ""


def test_question_voice_matches_displayed_prompt_after_consent(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call = client.post("/calls", json={"phone": "+821000000001"}).json()
        response = client.post(f"/calls/{call['id']}/consent", json={"text": "동의"}).json()
        audio = client.get(f"/calls/{call['id']}/prompts/question/audio")
        assert audio.status_code == 200
        assert speech.last_text == response["question_prompt"]


def test_location_consent_voice_matches_displayed_prompt(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).json()
        assert client.get(f"/calls/{call_id}/prompts/ready/audio").status_code == 200
        assert speech.last_text == response["prompt"]


@pytest.mark.parametrize("kind", [
    "location_retry", "location_declined", "location_unavailable", "hospital_unavailable",
    "hospital_followup", "service_retry", "listening_retry",
])
def test_location_prompts_require_consent_and_speak_after_acceptance(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        assert client.get(f"/calls/{call_id}/prompts/{kind}/audio").status_code == 403
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        response = client.get(f"/calls/{call_id}/prompts/{kind}/audio")
        assert response.status_code == 200
        assert speech.last_text


def test_goodbye_can_be_spoken_before_consent(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        assert client.get(f"/calls/{call_id}/prompts/goodbye/audio").status_code == 200
        assert "마칠게요" in speech.last_text


def test_speech_unavailable_prompt_can_be_spoken_before_consent(tmp_path: Path) -> None:
    path = tmp_path / "speech-unavailable.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.get(f"/calls/{call_id}/prompts/speech_unavailable/audio")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"


def test_pesticide_emergency_voice_is_available_during_urgent_call(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/urgent")
        assert client.get(f"/calls/{call_id}/prompts/pesticide_urgent/audio").status_code == 200
        assert speech.last_text == PESTICIDE_EMERGENCY_MESSAGE
        assert client.get(f"/calls/{call_id}/prompts/goodbye/audio").status_code == 409


def test_ambiguous_and_declined_consent_have_spoken_prompts(tmp_path: Path) -> None:
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingSpeech()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        retry = client.get(f"/calls/{call_id}/prompts/consent_retry/audio")
        assert retry.status_code == 200
        assert "동의" in speech.last_text
        decision = client.post(f"/calls/{call_id}/consent", json={"text": "동의하지 않아요"}).json()
        declined = client.get(f"/calls/{call_id}/prompts/declined/audio")
        assert declined.status_code == 200
        assert speech.last_text == decision["prompt"]
