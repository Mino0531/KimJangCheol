"""Region search requires consent and never guesses among duplicate names."""

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_dashboard import RecommendationOutbox
from app.region_resolution import (
    RegionIndex,
    RegionRecord,
    public_region_index,
    region_router,
)
from app.services import MockLLM, MockTTS


def index() -> RegionIndex:
    return RegionIndex((
        RegionRecord(code="4680060000", province="전남광주통합특별시", district="화순군", locality="동면", latitude=35.0, longitude=127.0),
        RegionRecord(code="4280030000", province="강원특별자치도", district="철원군", locality="동면", latitude=38.0, longitude=127.2),
        RegionRecord(code="2971030000", province="전남광주통합특별시", district="광산구", locality="삼도동", latitude=35.1, longitude=126.7),
        RegionRecord(code="4780030000", province="경상북도", district="영천시", locality="봉산면", latitude=36.0, longitude=128.0),
        RegionRecord(code="4710010000", province="경상북도", district="다른시", locality="화순동", latitude=36.1, longitude=128.1),
        RegionRecord(code="4115010000", province="경기도", district="광주시", locality="삼도동", latitude=37.1, longitude=127.1),
    ))


def test_duplicate_locality_has_no_coordinates() -> None:
    result = index().resolve("동면")
    assert result.status == "ambiguous"
    assert result.match is None
    assert {(c.province, c.district) for c in result.candidates} == {("전라남도", "화순군"), ("강원특별자치도", "철원군")}


def test_spoken_short_district_and_province_resolve_one_location() -> None:
    result = index().resolve("전남 화순 동면")
    assert result.status == "matched"
    assert result.match is not None
    assert (result.match.code, result.match.location_precision) == ("4680060000", "region_reference")


def test_contradictory_district_does_not_select_another_region() -> None:
    assert index().resolve("화순 봉산면").status == "not_found"


def test_matching_spoken_province_uses_administrative_code() -> None:
    result = index().resolve("광주 삼도동")
    assert result.match is not None
    assert result.match.province == "광주광역시"


def test_district_filter_precedes_overlapping_longer_locality() -> None:
    result = index().resolve("전남 화순 동면")
    assert result.match is not None
    assert result.match.district == "화순군"


def test_public_workbook_loads_and_resolves_real_regions() -> None:
    actual = public_region_index()
    assert len(actual.records) > 3000
    assert actual.resolve("전남 화순 동면").status == "matched"
    assert actual.resolve("동면").status == "ambiguous"
    assert actual.resolve("광주 삼도동").status == "matched"


def make_client(tmp_path: Path, region_index: RegionIndex) -> tuple[TestClient, Database, str]:
    database = Database(tmp_path / "regions.sqlite")
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session("+821000000000")["id"])
    calls.create(call_id)

    def auth(authorization: str | None = Header(default=None)) -> None:
        if authorization != "Bearer test":
            raise HTTPException(401, "Authentication required")

    context = SimpleNamespace(database=database, auth=auth)
    app = FastAPI()
    app.include_router(region_router(context, region_index))
    return TestClient(app), database, call_id


def test_region_route_requires_call_and_location_consent(tmp_path: Path) -> None:
    client, database, call_id = make_client(tmp_path, index())
    with client:
        url = f"/calls/{call_id}/regions/resolve"
        assert client.post(url, json={"text": "화순 동면"}).status_code == 401
        assert client.post(url, json={"text": "화순 동면"}, headers={"Authorization": "Bearer test"}).status_code == 403
        assert CallStore(database).decide(call_id, "accepted")
        response = client.post(url, json={"text": "화순 동면"}, headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["match"]["location_precision"] == "region_reference"
        RecommendationOutbox(database).set_location_consent(call_id, False)
        assert client.post(url, json={"text": "화순 동면"}, headers={"Authorization": "Bearer test"}).status_code == 403


def test_region_route_rechecks_consent_after_lookup(tmp_path: Path, monkeypatch) -> None:
    region_index = index()
    client, database, call_id = make_client(tmp_path, region_index)
    assert CallStore(database).decide(call_id, "accepted")
    resolve = region_index.resolve

    def revoke_during_lookup(text: str):
        result = resolve(text)
        RecommendationOutbox(database).set_location_consent(call_id, False)
        return result

    monkeypatch.setattr(region_index, "resolve", revoke_during_lookup)
    with client:
        response = client.post(f"/calls/{call_id}/regions/resolve", json={"text": "화순 동면"}, headers={"Authorization": "Bearer test"})
        assert response.status_code == 403


def test_delayed_region_input_cannot_cross_location_consent_generations(tmp_path: Path) -> None:
    client, database, call_id = make_client(tmp_path, index())
    assert CallStore(database).decide(call_id, "accepted")
    outbox = RecommendationOutbox(database)
    revoked = outbox.set_location_consent_with_epoch(call_id, False)
    assert revoked is not None
    assert outbox.set_location_consent(call_id, True, source_location_generation=0,
                                       source_location_revision=revoked[3]) is not None
    with client:
        url = f"/calls/{call_id}/regions/resolve"
        headers = {"Authorization": "Bearer test"}
        assert client.post(url, json={"text": "화순 동면", "source_location_generation": 1}, headers=headers).status_code == 409
        assert client.post(url, json={"text": "화순 동면"}, headers=headers).status_code == 409
        current = client.post(url, json={"text": "화순 동면", "source_location_generation": 2}, headers=headers)
        assert current.status_code == 200
        assert current.json()["location_generation"] == 2


def test_legacy_region_request_allowed_before_location_consent_changes(tmp_path: Path) -> None:
    client, database, call_id = make_client(tmp_path, index())
    assert CallStore(database).decide(call_id, "accepted")
    with client:
        response = client.post(f"/calls/{call_id}/regions/resolve", json={"text": "화순 동면"},
                               headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        assert response.json()["location_generation"] == 1


def test_generation_change_during_lookup_discards_coordinates(tmp_path: Path, monkeypatch) -> None:
    region_index = index()
    client, database, call_id = make_client(tmp_path, region_index)
    assert CallStore(database).decide(call_id, "accepted")
    resolve = region_index.resolve

    def revoke_and_reaccept(text: str):
        result = resolve(text)
        outbox = RecommendationOutbox(database)
        revoked = outbox.set_location_consent_with_epoch(call_id, False)
        assert revoked is not None
        assert outbox.set_location_consent(call_id, True, source_location_generation=0,
                                           source_location_revision=revoked[3]) is not None
        return result

    monkeypatch.setattr(region_index, "resolve", revoke_and_reaccept)
    with client:
        response = client.post(f"/calls/{call_id}/regions/resolve",
                               json={"text": "화순 동면", "source_location_generation": 1},
                               headers={"Authorization": "Bearer test"})
        assert response.status_code == 409
        assert "match" not in response.json()


def test_location_reset_requires_explicit_new_generation(tmp_path: Path) -> None:
    from app.call_facts import initialize as initialize_facts
    from app.call_utterances import PendingUtterances

    client, database, call_id = make_client(tmp_path, index())
    assert CallStore(database).decide(call_id, "accepted")
    initialize_facts(database)
    utterances = PendingUtterances(database)
    utterances.initialize()
    assert utterances.admit(call_id, "reset-a", "어머니 병원을 찾아주세요", "general")
    utterances.classified(call_id, "reset-a", "recommend", expects_turn=False)
    assert RecommendationOutbox(database).reset_location_generation(call_id, "reset-a") == 2
    with client:
        url = f"/calls/{call_id}/regions/resolve"
        headers = {"Authorization": "Bearer test"}
        assert client.post(url, json={"text": "화순 동면"}, headers=headers).status_code == 409
        assert client.post(url, json={"text": "화순 동면", "source_location_generation": 1}, headers=headers).status_code == 409
        assert client.post(url, json={"text": "화순 동면", "source_location_generation": 2}, headers=headers).status_code == 200


def test_region_lookup_rejects_stale_consent_revision(tmp_path: Path) -> None:
    client, database, call_id = make_client(tmp_path, index())
    assert CallStore(database).decide(call_id, "accepted")
    with closing(database.connect()) as connection:
        initial_revision = int(connection.execute("SELECT MAX(id) FROM location_consent_events WHERE call_id=?", (call_id,)).fetchone()[0])
    outbox = RecommendationOutbox(database)
    revoked = outbox.set_location_consent_with_epoch(call_id, False)
    assert revoked is not None
    accepted = outbox.set_location_consent_with_epoch(call_id, True, source_location_generation=0,
                                                      source_location_revision=revoked[3])
    assert accepted is not None
    with client:
        url = f"/calls/{call_id}/regions/resolve"
        headers = {"Authorization": "Bearer test"}
        data = {"text": "화순 동면", "source_location_generation": 2}
        assert client.post(url, json={**data, "source_location_revision": initial_revision}, headers=headers).status_code == 409
        response = client.post(url, json={**data, "source_location_revision": accepted[3]}, headers=headers)
        assert response.status_code == 200
        assert response.json()["location_revision"] == accepted[3]


def test_real_application_region_flow_and_revocation(tmp_path: Path) -> None:
    path = tmp_path / "real-app.sqlite"
    settings = Settings(environment="development", service_token="test-token", database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        created = client.post("/calls", json={"phone": "+821000000000"})
        assert created.status_code == 201
        call_id = created.json()["id"]
        assert client.post(f"/calls/{call_id}/consent", json={"text": "어 동의했어"}).json()["state"] == "accepted"
        url = f"/calls/{call_id}/regions/resolve"
        assert client.post(url, json={"text": "동면"}).json()["status"] == "ambiguous"
        found = client.post(url, json={"text": "전남 화순 동면"}).json()
        assert found["status"] == "matched" and found["match"]["district"] == "화순군"
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": False}).status_code == 200
        assert client.post(url, json={"text": "전남 화순 동면"}).status_code == 403
