"""Independent configuration of Gemini's reasoning and waiting models."""

import pytest

from app.providers import build_services
from app.services import MockLLM, OpenAICompatibleLLM, ServiceError, SplitLLM


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HOSPITAL_LOW_PROVIDER", "HOSPITAL_HIGH_PROVIDER", "HOSPITAL_TTS_PROVIDER",
        "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_HIGH_MODEL", "HOSPITAL_ALLOW_MEDICAL_DATA",
        "GEMINI_HIGH_REASONING_EFFORT",
        "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_gemini_high_uses_same_key_with_independent_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("HOSPITAL_ALLOW_MEDICAL_DATA", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-low")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    services = build_services()
    llm = services.llm
    assert isinstance(llm, SplitLLM)
    assert isinstance(llm.low_provider, OpenAICompatibleLLM)
    assert isinstance(llm.high_provider, OpenAICompatibleLLM)
    assert llm.low_provider.config.low_model == "test-low"
    assert llm.high_provider.config.reasoning_model == "test-high"
    assert llm.high_provider.config.allow_medical_data
    assert llm.high_provider.config.reasoning_effort == "high"
    assert llm.low_provider.config.reasoning_effort is None
    assert llm.high_provider.config.timeout_seconds == 120.0
    assert llm.low_provider.config.timeout_seconds == 30.0
    assert services.reasoning_timeout == 130.0
    assert llm.high_provider.config.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_gemini_high_accepts_configured_reasoning_effort(monkeypatch: pytest.MonkeyPatch, effort: str) -> None:
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("HOSPITAL_ALLOW_MEDICAL_DATA", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    monkeypatch.setenv("GEMINI_HIGH_REASONING_EFFORT", effort)
    llm = build_services().llm
    assert isinstance(llm, SplitLLM)
    assert isinstance(llm.low_provider, OpenAICompatibleLLM)
    assert isinstance(llm.high_provider, OpenAICompatibleLLM)
    assert llm.low_provider.config.reasoning_effort is None
    assert llm.high_provider.config.reasoning_effort == effort


@pytest.mark.parametrize("effort", ["minimal", "unexpected", ""])
def test_gemini_high_rejects_unsupported_reasoning_effort(monkeypatch: pytest.MonkeyPatch, effort: str) -> None:
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("HOSPITAL_ALLOW_MEDICAL_DATA", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    monkeypatch.setenv("GEMINI_HIGH_REASONING_EFFORT", effort)
    with pytest.raises(ServiceError):
        build_services()


def test_mock_high_ignores_gemini_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "mock")
    monkeypatch.setenv("GEMINI_HIGH_REASONING_EFFORT", "unexpected")
    llm = build_services().llm
    assert isinstance(llm, SplitLLM)
    assert isinstance(llm.high_provider, MockLLM)


def test_gemini_high_requires_explicit_medical_transfer_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    with pytest.raises(ServiceError):
        build_services()


@pytest.mark.parametrize("missing", ["GEMINI_API_KEY", "GEMINI_HIGH_MODEL"])
def test_gemini_high_requires_both_key_and_model(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setenv("HOSPITAL_LOW_PROVIDER", "mock")
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("HOSPITAL_ALLOW_MEDICAL_DATA", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    monkeypatch.delenv(missing)
    with pytest.raises(ServiceError):
        build_services()


def test_low_can_be_mock_with_gemini_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_LOW_PROVIDER", "mock")
    monkeypatch.setenv("HOSPITAL_HIGH_PROVIDER", "gemini")
    monkeypatch.setenv("HOSPITAL_ALLOW_MEDICAL_DATA", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_HIGH_MODEL", "test-high")
    llm = build_services().llm
    assert isinstance(llm, SplitLLM)
    assert isinstance(llm.low_provider, MockLLM)
    assert isinstance(llm.high_provider, OpenAICompatibleLLM)


def test_reasoning_effort_only_sent_for_reasoning() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import anyio

    from app.services import OpenAIConfig

    bodies: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            bodies.append(body)
            content = "A" if len(bodies) == 1 else "[clinic:1]"
            payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        def serve_two() -> None:
            server.handle_request()
            server.handle_request()

        config = OpenAIConfig(
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="test",
            low_model="same-model", reasoning_model="same-model", reasoning_effort="high",
            allow_medical_data=True,
        )
        worker = threading.Thread(target=serve_two, daemon=True)
        worker.start()
        provider = OpenAICompatibleLLM(config)
        anyio.run(provider.low, "question")
        anyio.run(provider.reasoning, "question", [{"id": "clinic:1", "text": "source"}])
        worker.join(timeout=5)
    assert "reasoning_effort" not in bodies[0]
    assert bodies[1]["reasoning_effort"] == "high"
