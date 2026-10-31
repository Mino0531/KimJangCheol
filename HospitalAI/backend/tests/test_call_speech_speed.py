from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from app.call_prompts import render_prompt
from app.config import Settings
from app.db import Database
from app.elevenlabs import ElevenLabsConfig, ElevenLabsTTS
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_api import add_audio
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import MockLLM, TTSResult
from test_call_capabilities import create_public_call, make_client


class RecordingElevenLabs(ElevenLabsTTS):
    def __init__(self) -> None:
        super().__init__(ElevenLabsConfig(api_key="test-key", voice_id="test-voice"))
        self.calls: list[tuple[str, float | None]] = []

    async def deliver(self, text: str, kind: str, *, speed: float | None = None) -> TTSResult:
        self.calls.append((kind, speed))
        return TTSResult("synthesized", kind, text, False, b"ID3test", "audio/mpeg")


def test_prompt_uses_requested_speed() -> None:
    speech = RecordingElevenLabs()
    response = anyio.run(lambda: render_prompt(speech, "question", speed=0.85))
    assert response.status_code == 200
    assert speech.calls == [("prompt", 0.85)]


def test_hospital_audio_uses_per_call_speed_and_identifies_area_reference() -> None:
    speech = RecordingElevenLabs()
    result = recommend(RecommendationQuery(latitude=35.1, longitude=126.9), [
        HospitalCandidate(id="a", name="가상병원", latitude=35.11, longitude=126.9, source="test")])
    response = anyio.run(lambda: add_audio(result, speech, 3, speed=0.85,
                                         location_precision="region_reference", region_label="화순읍"))
    assert response.audio_status == "synthesized"
    assert speech.calls == [("recommendation", 0.85)]
    assert "화순읍" in response.speech_text


def test_initial_hospital_announcement_is_short_and_defers_phone_number() -> None:
    speech = RecordingElevenLabs()
    result = recommend(RecommendationQuery(latitude=35.1, longitude=126.9), [
        HospitalCandidate(id="a", name="가상병원", phone="062-123-4567", latitude=35.11, longitude=126.9, source="test")])
    response = anyio.run(lambda: add_audio(result, speech, 3, location_precision="region_reference", region_label="화순읍"))
    assert response.speech_text is not None
    assert "062-123-4567" not in response.speech_text
    assert response.speech_text.count(".") <= 2


def test_speed_is_per_call_and_requires_live_accepted_consent(tmp_path: Path) -> None:
    path = tmp_path / "speed.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingElevenLabs()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        first = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        second = client.post("/calls", json={"phone": "+821000000002"}).json()["id"]
        assert client.post(f"/calls/{first}/speech-speed", json={"speed": "slow"}).status_code == 409
        for call_id in (first, second):
            assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
        assert client.post(f"/calls/{first}/speech-speed", json={"speed": "slow"}).json()["speed"] == "slow"
        assert client.get(f"/calls/{first}/prompts/question/audio").status_code == 200
        assert client.get(f"/calls/{second}/prompts/question/audio").status_code == 200
        assert speech.calls[-2:] == [("prompt", 0.85), ("prompt", 1.0)]
        assert client.post(f"/calls/{first}/end", json={"reason": "caller"}).status_code == 200
        assert client.post(f"/calls/{first}/speech-speed", json={"speed": "normal"}).status_code == 409


def test_final_answer_synthesis_uses_call_speed(tmp_path: Path) -> None:
    path = tmp_path / "final-speed.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingElevenLabs()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
        assert client.post(f"/calls/{call_id}/speech-speed", json={"speed": "slow"}).status_code == 200
        assert client.post(f"/calls/{call_id}/messages", json={"request_id": "one", "text": "병원 갈 때 준비물은요?"}).status_code == 202
        assert ("final", 0.85) in speech.calls


def test_speed_cannot_be_changed_with_another_calls_capability(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = create_public_call(client)
        second = create_public_call(client)
        first_auth = {"Authorization": f"Bearer {first['call_token']}"}
        second_auth = {"Authorization": f"Bearer {second['call_token']}"}
        assert client.post(f"/calls/{second['id']}/consent", json={"text": "동의"}, headers=second_auth).status_code == 200
        response = client.post(f"/calls/{second['id']}/speech-speed", json={"speed": "slow"}, headers=first_auth)
        assert response.status_code == 401


@pytest.mark.parametrize("kind", ["hold", "region_request", "region_ambiguous", "region_unavailable",
                                  "region_confirmed", "silence_check", "speed_slow", "speed_normal", "patient_reconfirm",
                                  "context_unavailable"])
def test_new_prompts_require_consent(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "prompts.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    speech = RecordingElevenLabs()
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), speech))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        assert client.get(f"/calls/{call_id}/prompts/{kind}/audio").status_code == 403
        assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
        assert client.get(f"/calls/{call_id}/prompts/{kind}/audio").status_code == 200
