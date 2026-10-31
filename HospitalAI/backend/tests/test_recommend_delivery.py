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


def test_location_consent_is_persisted_and_revocation_cancels_pending_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    async def fake_recommendations(query, service_key):
        return sample_result()

    monkeypatch.setattr("app.call_api.public_recommendations", fake_recommendations)
    database_path = tmp_path / "revocation.sqlite"
    database = Database(database_path)
    settings = Settings(environment="development", database_path=str(database_path), service_token="test-token")
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        with closing(database.connect()) as con, con:
            con.execute("UPDATE calls SET consent_version='municipality-v2' WHERE id=?", (call_id,))
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        request = {"latitude": 35.123456, "longitude": 126.654321, "specialty": "내과", "location_consent": True}
        assert client.post(f"/calls/{call_id}/recommendations", json=request).status_code == 403
        accepted = client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"
        body = client.post(f"/calls/{call_id}/recommendations", json=request).json()
        event_id = body["dashboard_event_id"]
        revoked = client.post(f"/calls/{call_id}/location-consent", json={"accepted": False})
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
        assert revoked.json()["cancelled"] == 1
        assert client.post(f"/calls/{call_id}/recommendations", json=request).status_code == 403
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": True, "source_location_generation": 0,
            "source_location_revision": revoked.json()["location_revision"]}).status_code == 200
        again = client.post(f"/calls/{call_id}/recommendations", json={**request, "source_location_generation": 2}).json()
        assert again["dashboard_event_id"] != event_id
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": False}).status_code == 200
    outbox = RecommendationOutbox(database)
    assert outbox.state(call_id, event_id) == "cancelled"
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is None
    requests: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(500, request=request)

    async def scenario() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"), transport=httpx2.MockTransport(handle))

    anyio.run(scenario)
    assert requests == []


def test_emergency_after_event_selection_prevents_network_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, call_id = setup_call(tmp_path)
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    original_next = outbox._next

    def select_then_emergency():
        item = original_next()
        if item is not None:
            turn, _ = database.create_turn(call_id, "urgent-after-selection", "농약 뿌리고 어지러워요")
            database.update_turn(str(turn["id"]), {"status": "emergency", "final_text": "긴급 안내"})
        return item

    monkeypatch.setattr(outbox, "_next", select_then_emergency)
    requests: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(500, request=request)

    async def scenario() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"), transport=httpx2.MockTransport(handle))

    anyio.run(scenario)
    assert requests == []


@pytest.mark.parametrize("failure", ["corrupted", "client_error", "invalid_ack"])
def test_permanent_failure_does_not_starve_later_events(tmp_path: Path, failure: str) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, first_call = setup_call(tmp_path)
    second_call = str(database.create_session("+821000000002")["id"])
    CallStore(database).create(second_call)
    assert CallStore(database).decide(second_call, "accepted")
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(first_call, True)
    outbox.set_location_consent(second_call, True)
    first = outbox.enqueue(first_call, sample_result(), specialty="내과", location_consent=True)
    second = outbox.enqueue(second_call, sample_result(), specialty="내과", location_consent=True)
    assert first is not None and second is not None
    if failure == "corrupted":
        with closing(database.connect()) as con, con:
            row = con.execute("SELECT body FROM recommendation_outbox WHERE event_id = ?", (first[0],)).fetchone()
            body = json.loads(row[0])
            body["call_id"] = "other-call"
            con.execute("UPDATE recommendation_outbox SET body = ? WHERE event_id = ?", (json.dumps(body), first[0]))
    requests: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body["event_id"])
        if body["event_id"] == first[0] and failure == "client_error":
            return httpx2.Response(422, request=request)
        if body["event_id"] == first[0] and failure == "invalid_ack":
            return httpx2.Response(201, json={"applied": True, "duplicate": False, "event_id": "wrong", "call_id": first_call}, request=request)
        return httpx2.Response(201, json={"applied": True, "duplicate": False, "event_id": second[0], "call_id": second_call}, request=request)

    async def scenario() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"), transport=httpx2.MockTransport(handle))

    anyio.run(scenario)
    assert outbox.state(first_call, first[0]) == "failed"
    assert outbox.state(second_call, second[0]) == "sent"
    assert second[0] in requests


def test_rate_limit_keeps_head_pending_for_retry(tmp_path: Path) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database, first_call = setup_call(tmp_path)
    second_call = str(database.create_session("+821000000002")["id"])
    CallStore(database).create(second_call)
    assert CallStore(database).decide(second_call, "accepted")
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    outbox.set_location_consent(first_call, True)
    outbox.set_location_consent(second_call, True)
    first = outbox.enqueue(first_call, sample_result(), specialty="내과", location_consent=True)
    second = outbox.enqueue(second_call, sample_result(), specialty="내과", location_consent=True)
    assert first is not None and second is not None
    requests: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content)["event_id"])
        return httpx2.Response(429, request=request)

    async def scenario() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"), transport=httpx2.MockTransport(handle))

    anyio.run(scenario)
    assert requests == [first[0]]
    assert outbox.state(first_call, first[0]) == "pending"
    assert outbox.state(second_call, second[0]) == "pending"


def test_reconsent_during_search_does_not_authorize_old_search_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recommend_dashboard import RecommendationOutbox

    database_path = tmp_path / "reconsent.sqlite"
    database = Database(database_path)
    settings = Settings(environment="development", database_path=str(database_path), service_token="test-token")
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    call_id = ""

    async def fake_recommendations(query, service_key):
        outbox = RecommendationOutbox(database)
        revoked = outbox.set_location_consent_with_epoch(call_id, False)
        assert revoked is not None
        outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=revoked[3])
        return sample_result()

    monkeypatch.setattr("app.call_api.public_recommendations", fake_recommendations)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True})
    assert response.status_code == 409
    with closing(database.connect()) as con:
        assert con.execute("SELECT COUNT(*) FROM recommendation_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("event", ["emergency", "revoke"])
def test_emergency_or_revocation_during_audio_never_returns_old_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    from app.recommend_api import RecommendationResponse
    from app.recommend_dashboard import RecommendationOutbox

    database_path = tmp_path / "during-audio.sqlite"
    database = Database(database_path)
    settings = Settings(environment="development", database_path=str(database_path), service_token="test-token")
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    call_id = ""

    async def fake_recommendations(query, service_key):
        return sample_result()

    async def fake_audio(result, tts, timeout, *, specialty_requested=False):
        if event == "emergency":
            turn, _ = database.create_turn(call_id, "during-audio", "농약 뿌리고 어지러워요")
            database.update_turn(str(turn["id"]), {"status": "emergency", "final_text": "긴급 안내"})
        else:
            RecommendationOutbox(database).set_location_consent(call_id, False)
        return RecommendationResponse(**result.model_dump())

    monkeypatch.setattr("app.call_api.public_recommendations", fake_recommendations)
    monkeypatch.setattr("app.call_api.add_audio", fake_audio)
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True, "include_audio": True})
    if event == "revoke":
        assert response.status_code == 403
    else:
        assert response.status_code == 200
        assert response.json()["status"] == "urgent"
        assert response.json()["hospitals"] == []
