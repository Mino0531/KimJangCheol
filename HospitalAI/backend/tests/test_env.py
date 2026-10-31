"""Checks for backend .env loading and runtime provider configuration."""

import os
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app import config, diagnostics, main
from app.elevenlabs import ElevenLabsTTS
from app.orchestrator import ServiceBundle
from app.providers import build_services
from app.services import MockLLM, MockTTS, OpenAICompatibleLLM, ServiceError, SplitLLM


def test_load_environment_uses_backend_file_when_cwd_changes(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Given an app-specific env file outside the current directory.
    env_file = tmp_path / "backend" / ".env"
    env_file.parent.mkdir()
    env_file.write_text('GEMINI_API_KEY="example-secret"\n', encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.chdir(tmp_path)

    # When the standard loader runs.
    config.load_environment()

    # Then the key comes from the app's file, regardless of cwd.
    assert os.environ["GEMINI_API_KEY"] == "example-secret"


def test_load_environment_preserves_existing_process_values(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Given conflicting values in the environment and the file.
    env_file = tmp_path / ".env"
    env_file.write_text("ELEVENLABS_API_KEY=file-secret\n", encoding="utf-8")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "process-secret")

    # When the file is read.
    config.load_environment(env_file)

    # Then the process environment remains authoritative.
    assert os.environ["ELEVENLABS_API_KEY"] == "process-secret"


def test_default_app_loads_file_before_creating_services(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    # Given settings and provider credentials available only in .env.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HOSPITAL_ENV=development\n"
        "HOSPITAL_SERVICE_TOKEN=test-token\n"
        "GEMINI_API_KEY=example-secret\n"
        "ELEVENLABS_API_KEY=example-secret\n"
        "ELEVENLABS_VOICE_ID=testvoice\n"
        "HOSPITAL_ALLOW_TTS_MEDICAL_DATA=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    for name in (
        "HOSPITAL_ENV", "HOSPITAL_SERVICE_TOKEN", "HOSPITAL_LOW_PROVIDER", "GEMINI_API_KEY",
        "HOSPITAL_TTS_PROVIDER", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
        "HOSPITAL_ALLOW_TTS_MEDICAL_DATA",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOSPITAL_DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "mock")

    # When the default application is created without external calls.
    built: list[ServiceBundle] = []
    original_build = main.build_services

    def capture_services() -> ServiceBundle:
        services = original_build()
        built.append(services)
        return services

    monkeypatch.setattr(main, "build_services", capture_services)
    app = main.default_app()

    # Then its authentication and provider configuration have consumed the file.
    with TestClient(app) as client:
        response = client.post(
            "/sessions", headers={"Authorization": "Bearer test-token"},
            json={"phone": "+821000000001"},
        )
    assert response.status_code == 200
    assert isinstance(built[0].tts, ElevenLabsTTS)
    assert isinstance(built[0].llm, SplitLLM)
    assert isinstance(built[0].llm.low_provider, OpenAICompatibleLLM)


def test_auto_provider_uses_mocks_when_credentials_absent(monkeypatch: MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("HOSPITAL_LOW_PROVIDER", "auto")
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "auto")
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "mock")
    services = build_services()
    assert isinstance(services.tts, MockTTS)
    assert isinstance(services.llm, SplitLLM)
    assert isinstance(services.llm.low_provider, MockLLM)


def test_auto_provider_rejects_incomplete_elevenlabs_credentials(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_LOW_PROVIDER", "mock")
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "mock")
    monkeypatch.setenv("HOSPITAL_TTS_PROVIDER", "auto")
    monkeypatch.setenv("HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "example-secret")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "")
    with pytest.raises(ServiceError):
        build_services()


def test_diagnostics_reads_file_before_creating_services(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=example-secret\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY")

    def fake_services() -> ServiceBundle:
        assert os.environ.get("GEMINI_API_KEY") == "example-secret"
        return ServiceBundle(MockLLM(), MockTTS())

    monkeypatch.setattr(diagnostics, "build_services", fake_services)
    anyio.run(diagnostics.check)
