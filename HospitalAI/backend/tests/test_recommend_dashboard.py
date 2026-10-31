"""Verify consented, privacy-limited recommendation delivery with real SQLite."""

import json
from contextlib import closing
from pathlib import Path

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.hospital_bridge import HospitalConfig
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import MockLLM, MockTTS


def setup_call(tmp_path: Path, *, accepted: bool = True) -> tuple[Database, str]:
    database = Database(tmp_path / "test.sqlite")
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session("+821000000001")["id"])
    calls.create(call_id)
    if accepted:
        assert calls.decide(call_id, "accepted")
    return database, call_id


def sample_result():
    query = RecommendationQuery(latitude=35.123456, longitude=126.654321, specialty="내과")
    hospital = HospitalCandidate(
        id="public-hospital-1", name="가상 병원", address="가상 주소", phone="062-000-0000",
        latitude=35.2, longitude=126.6, specialties=("내과",), specialists={"내과": 2},
        hours={"mon": ("0900", "1700")}, source="가상 공공 데이터",
    )
    return recommend(query, [hospital])


def test_enqueues_only_with_call_and_location_consent_and_excludes_coordinates(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path, accepted=False)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is None
    assert CallStore(database).decide(call_id, "accepted")
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=False) is None
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    event_id, state = queued
    assert state == "pending"
    with closing(database.connect()) as con:
        row = con.execute("SELECT body FROM recommendation_outbox WHERE event_id = ?", (event_id,)).fetchone()
    body = json.loads(row[0])
    assert body["call_id"] == call_id and body["specialty"] == "내과"
    assert body["hospitals"][0]["name"] == "가상 병원"
    encoded = json.dumps(body, ensure_ascii=False)
    assert "latitude" not in encoded and "longitude" not in encoded and "audio" not in encoded
    assert "35.123456" not in encoded and "126.654321" not in encoded


def test_urgent_and_incomplete_calls_do_not_enqueue(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    turn, _ = database.create_turn(call_id, "urgent", "농약을 뿌렸는데 어지러워요")
    database.update_turn(str(turn["id"]), {"status": "emergency", "final_text": "긴급 안내"})
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is None


def test_repeated_same_recommendation_reuses_event_identity_and_payload(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    first = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    second = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert first is not None and second is not None
    assert first[0] == second[0]
    with closing(database.connect()) as con:
        assert con.execute("SELECT COUNT(*) FROM recommendation_outbox").fetchone()[0] == 1


def test_failed_delivery_retries_same_payload_and_valid_ack_marks_sent(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    event_id = queued[0]
    requests: list[bytes] = []
    paths: list[str] = []
    auth_headers: list[str | None] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.content)
        paths.append(request.url.path)
        auth_headers.append(request.headers.get("Authorization"))
        if len(requests) == 1:
            return httpx2.Response(503, request=request)
        return httpx2.Response(201, json={"applied": True, "duplicate": False, "event_id": event_id, "call_id": call_id}, request=request)

    transport = httpx2.MockTransport(handle)
    config = HospitalConfig("http://127.0.0.1:8767", "local-test-token")

    async def scenario() -> None:
        first = await outbox.dispatch(config, transport=transport)
        assert first.delivered == 0 and first.pending == 1
        assert outbox.state(call_id, event_id) == "pending"
        second = await outbox.dispatch(config, transport=transport)
        assert second.delivered == 1 and second.pending == 0

    anyio.run(scenario)
    assert requests[0] == requests[1]
    assert paths == ["/api/team/recommendation-results", "/api/team/recommendation-results"]
    assert auth_headers == ["Bearer local-test-token", "Bearer local-test-token"]
    assert outbox.state(call_id, event_id) == "sent"


def test_tampered_event_never_reaches_hospital(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    event_id = queued[0]
    with closing(database.connect()) as con, con:
        row = con.execute("SELECT body FROM recommendation_outbox WHERE event_id = ?", (event_id,)).fetchone()
        body = json.loads(row[0])
        body["call_id"] = "different-call"
        con.execute("UPDATE recommendation_outbox SET body = ? WHERE event_id = ?", (json.dumps(body), event_id))
    requests: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(500, request=request)

    async def scenario() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"), transport=httpx2.MockTransport(handle))

    anyio.run(scenario)
    assert requests == []
    assert outbox.state(call_id, event_id) == "failed"


def test_delivery_state_is_scoped_to_owning_call(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    other_id = str(database.create_session("+821000000002")["id"])
    CallStore(database).create(other_id)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    assert outbox.state(call_id, queued[0]) == "pending"
    assert outbox.state(other_id, queued[0]) is None


def test_http_recommendations_queue_without_coordinates_and_scope_delivery_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_recommendations(query, service_key):
        return sample_result()

    monkeypatch.setattr("app.call_api.public_recommendations", fake_recommendations)
    database_path = tmp_path / "http.sqlite"
    settings = Settings(environment="development", database_path=str(database_path), service_token="test-token")
    app = create_app(settings, Database(database_path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        other_id = client.post("/calls", json={"phone": "+821000000002"}).json()["id"]
        assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
        assert client.post(f"/calls/{other_id}/consent", json={"text": "동의"}).status_code == 200
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": True}).status_code == 200
        request = {"latitude": 35.123456, "longitude": 126.654321, "specialty": "내과", "location_consent": True}
        body = client.post(f"/calls/{call_id}/recommendations", json=request).json()
        assert body["status"] == "ok"
        assert body["dashboard_delivery"] == "disabled"
        event_id = body["dashboard_event_id"]
        assert client.get(f"/calls/{call_id}/recommendations/{event_id}/delivery").json()["state"] == "disabled"
        assert client.get(f"/calls/{other_id}/recommendations/{event_id}/delivery").status_code == 404
    with closing(Database(database_path).connect()) as con:
        persisted = con.execute("SELECT body FROM recommendation_outbox WHERE event_id = ?", (event_id,)).fetchone()[0]
    assert "35.123456" not in persisted and "126.654321" not in persisted


def test_emergency_arriving_during_search_blocks_recommendation_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "emergency-race.sqlite"
    database = Database(database_path)
    settings = Settings(environment="development", database_path=str(database_path), service_token="test-token")
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    call_id = ""

    async def fake_recommendations(query, service_key):
        turn, _ = database.create_turn(call_id, "urgent-during-search", "농약 뿌리고 어지러워요")
        database.update_turn(str(turn["id"]), {"status": "emergency", "final_text": "긴급 안내"})
        return sample_result()

    monkeypatch.setattr("app.call_api.public_recommendations", fake_recommendations)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True})
    assert response.status_code == 200
    assert response.json()["status"] == "urgent"
    assert response.json()["hospitals"] == []


