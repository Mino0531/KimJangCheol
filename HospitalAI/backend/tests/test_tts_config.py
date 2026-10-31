"""TTS provider selection must be deliberate and independent of model routing."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.elevenlabs import ElevenLabsTTS
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.providers import build_services
from app.services import MockLLM, MockTTS, ServiceError, TTSResult


@pytest.fixture(autouse=True)
def clear_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HOSPITAL_LOW_PROVIDER", "HOSPITAL_HIGH_PROVIDER", "HOSPITAL_TTS_PROVIDER",
        "HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
        "ELEVENLABS_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_tts_is_offline_mock() -> None:
    services = build_services()
    assert isinstance(services.tts, MockTTS)


def test_elevenlabs_requires_explicit_data_transfer_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice")
    with pytest.raises(ServiceError):
        build_services()


@pytest.mark.parametrize("missing", ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"])
def test_elevenlabs_requires_both_key_and_voice(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice")
    monkeypatch.delenv(missing)
    with pytest.raises(ServiceError):
        build_services()


def test_configured_elevenlabs_uses_flash_model_and_synthesis_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice")
    services = build_services()
    assert isinstance(services.tts, ElevenLabsTTS)
    assert services.tts.config.model_id == "eleven_flash_v2_5"
    assert services.tts_timeout == 30.0


def test_configured_elevenlabs_allows_explicit_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "test-voice")
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "eleven_v3")
    services = build_services()
    assert isinstance(services.tts, ElevenLabsTTS)
    assert services.tts.config.model_id == "eleven_v3"


def test_unrecognized_tts_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "unknown")
    with pytest.raises(ServiceError):
        build_services()


def test_internal_source_ids_are_not_exposed_but_sources_are_retained(tmp_path: Path) -> None:
    spoken: list[str] = []

    class SpeechCapture:
        async def deliver(self, text: str, kind: str) -> TTSResult:
            if kind == "final":
                spoken.append(text)
            return TTSResult(status="synthesized", kind=kind, text=text, interruptible=False)

    db = Database(tmp_path / "citation.sqlite3")
    settings = Settings(environment="development", service_token="service", seed_demo=True)
    application = create_app(settings, db, ServiceBundle(MockLLM(), SpeechCapture()))
    with TestClient(application, headers={"Authorization": "Bearer service"}) as client:
        session_id = client.post("/sessions", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/sessions/{session_id}/turns", json={"request_id": "1", "text": "기침이 있어요"})
        turn = client.get(f"/sessions/{session_id}/turns/1").json()

    assert "[clinic:" not in turn["final_text"]
    assert "clinic:" in turn["sources_json"]
    assert spoken and "[clinic:" not in spoken[0] and "[patient:" not in spoken[0]
