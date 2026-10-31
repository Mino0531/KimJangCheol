"""Natural consent speech remains explicit and excludes partial refusals."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


@pytest.mark.parametrize("speech", [
    "어 동의했어", "어, 동의했어요.", "음 네 동의할게요", "네 동의했습니다",
])
def test_natural_affirmation_enables_consultation_and_location(tmp_path: Path, speech: str) -> None:
    path = tmp_path / "calls.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.post(f"/calls/{call_id}/consent", json={"text": speech})
        assert response.status_code == 200
        assert response.json()["state"] == "accepted"
        with database.connect() as con:
            row = con.execute("SELECT state FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
            assert row is not None and row[0] == "accepted"


@pytest.mark.parametrize("speech", [
    "어 동의 안 했어", "동의한 적 없어", "동의했어?", "동의했어？",
    "어 동의했어, 근데 위치는 싫어", "위치는 싫어", "동의했다고 말하지 마",
    "동의했어 라고 말했어요", "동의했다는 뜻은 아니에요", "동의했어 그런데 싫어",
])
def test_ambiguous_or_partial_refusal_never_grants_consent(tmp_path: Path, speech: str) -> None:
    path = tmp_path / "calls.sqlite"
    settings = Settings(environment="development", service_token="service", database_path=str(path))
    with TestClient(create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.post(f"/calls/{call_id}/consent", json={"text": speech})
        assert response.status_code == 200
        assert response.json()["state"] != "accepted"
        assert client.post(f"/calls/{call_id}/messages", json={"request_id": "r", "text": "기침"}).status_code == 403
