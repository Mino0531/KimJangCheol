"""Consent mixed with another utterance stays explicit and consent-gated."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import call_consent
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


@pytest.mark.parametrize(
    ("spoken", "remainder"),
    [
        ("동의합니다. 어제부터 무릎이 아파요.", "어제부터 무릎이 아파요."),
        ("어, 동의했어. 병원 찾아주세요.", "병원 찾아주세요."),
        ("네. 병원이 어디에 있나요?", "병원이 어디에 있나요?"),
        ("동의합니다。 어제부터 무릎이 아파요.", "어제부터 무릎이 아파요."),
    ],
)
def test_explicit_consent_returns_followup_for_post_consent_processing(spoken: str, remainder: str) -> None:
    assert call_consent.split_consent(spoken) == ("accepted", remainder)


@pytest.mark.parametrize(
    "spoken",
    [
        "동의했어. 위치는 싫어",
        "동의합니다. 동의 취소해주세요",
        "동의합니다. 동의하지 않습니다",
        "동의합니다. 아니요 안 할래요",
        "동의합니다. 하지만 원하지 않습니다",
        "동의했다고 말했어요. 무릎이 아파요",
        "동의합니다라고 말했어요. 무릎이 아파요",
        "동의해도 되나요? 무릎이 아파요",
        "동의하지만 위치는 싫어요",
    ],
)
def test_uncertain_or_conditional_consent_never_returns_followup(spoken: str) -> None:
    decision, remainder = call_consent.split_consent(spoken)
    assert decision != "accepted"
    assert remainder is None


def test_refusal_never_returns_a_followup() -> None:
    assert call_consent.split_consent("동의하지 않습니다. 무릎이 아파요") == ("declined", None)


def test_http_mixed_consent_releases_followup_after_state_change(tmp_path: Path) -> None:
    database = Database(tmp_path / "mixed.sqlite")
    settings = Settings(environment="development", service_token="service", database_path=database.path)
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.post(f"/calls/{call_id}/consent", json={"text": "동의합니다. 어제부터 무릎이 아파요."})
        assert response.status_code == 200
        assert response.json()["state"] == "accepted"
        assert response.json()["followup_text"] == "어제부터 무릎이 아파요."


def test_http_urgent_speech_remains_preconsent_and_has_no_followup(tmp_path: Path) -> None:
    database = Database(tmp_path / "urgent.sqlite")
    settings = Settings(environment="development", service_token="service", database_path=database.path)
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer service"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        response = client.post(f"/calls/{call_id}/consent", json={"text": "동의합니다. 농약 때문에 어지러워요."})
        assert response.status_code == 200
        assert response.json()["state"] == "awaiting_consent"
        assert response.json()["urgent"] is True
        assert "followup_text" not in response.json()
