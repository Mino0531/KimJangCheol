"""Only staff-authorized handoff may send telephone data to a hospital."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import MockLLM, MockTTS


def test_hospital_retry_cannot_bypass_staff_handoff(tmp_path: Path) -> None:
    path = tmp_path / "hospital-guard.sqlite"
    settings = Settings(environment="development", database_path=str(path), service_token="service-token",
                        hospital_url="http://127.0.0.1:8767", hospital_token="hospital-token")
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service-token"}) as client:
        response = client.post("/calls/hospital/retry")
    assert response.status_code == 409


def test_recommendation_does_not_auto_send_to_hospital(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    async def stop_dispatch(*args, **kwargs):
        requested.append("dispatched")
        raise AssertionError("hospital dispatch must wait for staff handoff")

    async def search(query, service_key):
        candidate = HospitalCandidate(id="synthetic", name="가상 병원", address="가상 주소",
            phone="062-000-0000", latitude=35.2, longitude=126.6, specialties=("내과",),
            specialists={"내과": 1}, hours={"mon": ("0900", "1700")}, source="가상 자료")
        return recommend(RecommendationQuery(latitude=35.1, longitude=126.6, specialty="내과"), [candidate])

    monkeypatch.setattr("app.call_api.public_recommendations", search)
    monkeypatch.setattr("app.recommend_dashboard.RecommendationOutbox.dispatch", stop_dispatch)
    path = tmp_path / "recommendation-guard.sqlite"
    settings = Settings(environment="development", database_path=str(path), service_token="service-token",
                        hospital_url="http://127.0.0.1:8767", hospital_token="hospital-token")
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        response = client.post(f"/calls/{call_id}/recommendations", json={
            "latitude": 35.1, "longitude": 126.6, "specialty": "내과", "location_consent": True})
    assert response.status_code == 200
    assert response.json()["hospitals"]
    assert requested == []
