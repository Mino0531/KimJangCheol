"""Authenticated audio output integration tests using real temporary SQLite."""

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


class SyntheticTTS:
    async def deliver(self, text: str, kind: str) -> TTSResult:
        return TTSResult(status="synthesized", kind=kind, text=text, interruptible=False,
                         audio=b"audio-output:" + kind.encode(), mime_type="audio/mpeg")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "audio.sqlite3"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), SyntheticTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as test_client:
        yield test_client


def session_with_turn(client: TestClient) -> str:
    response = client.post("/sessions", json={"phone": "+821000000001", "verification_token": "verify"})
    assert response.status_code == 200
    session = response.json()["id"]
    created = client.post(f"/sessions/{session}/turns", json={"request_id": "r1", "text": "기침이 있어요"})
    assert created.status_code == 202
    return session


def test_synthesized_audio_is_available_over_authenticated_http(client: TestClient) -> None:
    session = session_with_turn(client)
    response = client.get(f"/sessions/{session}/turns/r1/audio/final")
    assert response.status_code == 200
    assert response.content == b"audio-output:final"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"


def test_audio_endpoint_rejects_wrong_bearer_token(client: TestClient) -> None:
    session = session_with_turn(client)
    response = client.get(f"/sessions/{session}/turns/r1/audio/final", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_audio_endpoint_cannot_resolve_another_sessions_turn(client: TestClient) -> None:
    session_with_turn(client)
    other = client.post("/sessions", json={"phone": "+821000000002", "verification_token": "verify"}).json()["id"]
    response = client.get(f"/sessions/{other}/turns/r1/audio/final")
    assert response.status_code == 404


def test_synthesis_status_does_not_claim_audio_was_played(client: TestClient) -> None:
    session = session_with_turn(client)
    turn = client.get(f"/sessions/{session}/turns/r1").json()
    assert json.loads(turn["tts_json"])["final"] == "synthesized"


def test_mock_tts_does_not_expose_audio_when_no_audio_was_synthesized(tmp_path: Path) -> None:
    path = tmp_path / "mock.sqlite3"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as test_client:
        session = session_with_turn(test_client)
        response = test_client.get(f"/sessions/{session}/turns/r1/audio/final")
    assert response.status_code == 404
