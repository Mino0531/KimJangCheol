"""Call-scoped recommendation access and uncertainty contracts."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_hira import HiraDetails, HiraError, HiraHospital, HiraTooBroad
from app.services import MockLLM, MockTTS, TTSResult


def general_hospital(**values):
    return HiraHospital(classification_code="11", **values)


class FakeClientContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


def make_client(tmp_path: Path, key: str = "") -> TestClient:
    path = tmp_path / "recommend.sqlite"
    settings = Settings(environment="development", service_token="test-token", seed_demo=True,
                        database_path=str(path), public_data_key=key)
    return TestClient(create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS())),
                      headers={"Authorization": "Bearer test-token"})


def call(client: TestClient, accepted: bool = True) -> str:
    response = client.post("/calls", json={"phone": "+821000000001"})
    call_id = response.json()["id"]
    if accepted:
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
    return call_id


def test_call_and_location_consent_required(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        pending = call(client, accepted=False)
        assert client.post(f"/calls/{pending}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True}).status_code == 403
        accepted = call(client)
        assert client.post(f"/calls/{accepted}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": False}).status_code == 403


def test_without_public_key_reports_unavailable(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call_id = call(client)
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True})
        assert response.status_code == 200
        assert response.json()["status"] == "unavailable"
        assert response.json()["hospitals"] == []


def test_stored_emergency_overrides_false_client_flag(tmp_path: Path) -> None:
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "urgent", "text": "농약을 뿌렸는데 어지러워요"})
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True, "urgent": False})
        assert response.status_code == 200
        assert response.json()["status"] == "urgent"
        assert response.json()["hospitals"] == []


def test_public_results_and_unknowns_are_not_fabricated(tmp_path: Path, monkeypatch) -> None:
    class FakeHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            return [general_hospital(id="a", name="가상 병원", address="주소", phone="123", latitude=35, longitude=126)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails(specialties=("내과",), specialists={"내과": 2}, hours=None)

    monkeypatch.setattr("app.recommend_api.HiraClient", FakeHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True, "specialty": "내과"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["hospitals"][0]["specialist_count"] is None
        assert body["facility_type"] == "general_hospital"
        assert body["hospitals"][0]["open_status"] == "unknown"
        assert body["hospitals"][0]["distance_km"] == 0


def test_api_outage_reports_unavailable(tmp_path: Path, monkeypatch) -> None:
    class BrokenHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            raise HiraError("outage")

    monkeypatch.setattr("app.recommend_api.HiraClient", BrokenHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True})
        assert response.json()["status"] == "unavailable"


def test_locations_not_written_into_consultation_history(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call_id = call(client)
        client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35.1241, "longitude": 126.3456, "location_consent": True})
        assert client.get(f"/sessions/{call_id}/history").json() == []


def test_pending_consultation_must_finish_before_ranking(tmp_path: Path) -> None:
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        database = Database(tmp_path / "recommend.sqlite")
        database.create_turn(call_id, "pending", "기침이 있어요")
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True})
        assert response.status_code == 409


def test_general_hospital_search_is_bounded_and_returns_nearest_limit(tmp_path: Path, monkeypatch) -> None:
    seen = []

    class DenseHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            seen.append(radius_m)
            if radius_m == 2000:
                raise HiraTooBroad("too many")
            return [general_hospital(id=str(i), name="가상 병원", address="주소", phone="123", latitude=35, longitude=126) for i in range(11)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails()

    monkeypatch.setattr("app.recommend_api.HiraClient", DenseHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert result["status"] == "ok"
        assert seen == [30000, 30000]
        assert result["search_radius_km"] == 30
        assert result["considered_count"] == 10
        assert result["total_in_radius"] is None


def test_sparse_general_hospital_results_do_not_include_other_facilities(tmp_path: Path, monkeypatch) -> None:
    seen = []

    class RuralHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            seen.append(radius_m)
            count = 12 if radius_m == 10000 else 2
            return [general_hospital(id=str(i), name="가상 병원", address="주소", phone="123", latitude=35, longitude=126) for i in range(count)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails()

    monkeypatch.setattr("app.recommend_api.HiraClient", RuralHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert seen == [30000, 30000]
        assert result["search_radius_km"] == 30
        assert result["considered_count"] == 2


def test_call_search_uses_hospital_classification_instead_of_a_department_filter(tmp_path: Path, monkeypatch) -> None:
    seen = []

    class SpecialtyHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int,
                              *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            seen.append((radius_m, specialty_code))
            if specialty_code == '05' and radius_m >= 5000:
                return [general_hospital(id='match', name='가상 병원', address='', phone='',
                                     latitude=35.03, longitude=126)]
            if specialty_code is None:
                return [general_hospital(id=str(i), name='가상 병원', address='', phone='',
                                     latitude=35, longitude=126) for i in range(15)]
            return []

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails(specialties=('정형외과',))

    monkeypatch.setattr('app.recommend_api.HiraClient', SpecialtyHira)
    with make_client(tmp_path, 'fake') as client:
        call_id = call(client)
        result = client.post(f'/calls/{call_id}/recommendations',
                             json={'latitude': 35, 'longitude': 126, 'location_consent': True,
                                   'specialty': '정형외과'}).json()
    assert seen == [(30000, None), (30000, None)]
    assert result['facility_type'] == 'general_hospital'
    assert result['specialty'] is None


def test_general_hospital_search_does_not_require_a_specific_department(tmp_path: Path, monkeypatch) -> None:
    class NoSpecialtyHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int,
                              *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            if specialty_code:
                return []
            return [general_hospital(id=str(i), name='가상 병원', address='', phone='',
                                 latitude=35, longitude=126) for i in range(10)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails()

    monkeypatch.setattr('app.recommend_api.HiraClient', NoSpecialtyHira)
    with make_client(tmp_path, 'fake') as client:
        call_id = call(client)
        result = client.post(f'/calls/{call_id}/recommendations',
                             json={'latitude': 35, 'longitude': 126, 'location_consent': True,
                                   'specialty': '정형외과'}).json()
    assert result['status'] == 'ok'
    assert result['facility_type'] == 'general_hospital'
    assert result['specialty'] is None


def test_department_source_error_does_not_fabricate_an_empty_search(tmp_path: Path, monkeypatch) -> None:
    class BrokenSpecialtyHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int,
                              *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            raise HiraError('provider failure')

    monkeypatch.setattr('app.recommend_api.HiraClient', BrokenSpecialtyHira)
    with make_client(tmp_path, 'fake') as client:
        call_id = call(client)
        result = client.post(f'/calls/{call_id}/recommendations',
                             json={'latitude': 35, 'longitude': 126, 'location_consent': True,
                                   'specialty': '정형외과'}).json()
    assert result['status'] == 'unavailable'
    assert result['hospitals'] == []


def test_optional_voice_uses_public_fields_without_coordinates_or_patient_text(tmp_path: Path, monkeypatch) -> None:
    class FakeHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            return [general_hospital(id=str(i), name="가상 병원", address="주소", phone="062-123-4567", latitude=latitude + .0001, longitude=longitude) for i in range(10)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails(specialties=("내과",), specialists={"내과": 2})

    class RecordingTTS:
        spoken = ""

        async def deliver(self, text: str, kind: str) -> TTSResult:
            self.spoken = text
            return TTSResult("synthesized", kind, text, False, b"mp3-example", "audio/mpeg")

    monkeypatch.setattr("app.recommend_api.HiraClient", FakeHira)
    path = tmp_path / "voice.sqlite"
    settings = Settings(environment="development", service_token="test-token", public_data_key="fake", database_path=str(path))
    voice = RecordingTTS()
    with TestClient(create_app(settings, Database(path), ServiceBundle(MockLLM(), voice)), headers={"Authorization": "Bearer test-token"}) as client:
        call_id = call(client)
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35.123456, "longitude": 126.123456, "location_consent": True, "specialty": "내과", "include_audio": True})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        result = response.json()
        assert result["status"] == "ok" and result["audio_status"] == "synthesized"
        assert result["audio_base64"] == "bXAzLWV4YW1wbGU="
        assert "35.123456" not in voice.spoken and "126.123456" not in voice.spoken
        assert "가상 병원" in voice.spoken


def test_general_hospital_classification_is_spoken_through_recommendation_endpoint(tmp_path: Path, monkeypatch) -> None:
    class FakeHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            return [general_hospital(id=str(i), name="가상 병원", address="주소", phone="123", latitude=35, longitude=126) for i in range(10)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails()

    class RecordingTTS:
        spoken = ""

        async def deliver(self, text: str, kind: str) -> TTSResult:
            self.spoken = text
            return TTSResult("synthesized", kind, text, False, b"mp3-example", "audio/mpeg")

    monkeypatch.setattr("app.recommend_api.HiraClient", FakeHira)
    path = tmp_path / "unknown-specialty.sqlite"
    settings = Settings(environment="development", service_token="test-token", public_data_key="fake", database_path=str(path))
    voice = RecordingTTS()
    with TestClient(create_app(settings, Database(path), ServiceBundle(MockLLM(), voice)), headers={"Authorization": "Bearer test-token"}) as client:
        call_id = call(client)
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126,
                               "location_consent": True, "specialty": "정형외과", "include_audio": True})
        assert response.status_code == 200
        assert response.json()["hospitals"][0]["specialty_match"] == "unknown"
        assert "종합병원으로 등록" in voice.spoken
        assert "전화로 확인" in voice.spoken


def test_voice_failure_does_not_discard_recommendations(tmp_path: Path, monkeypatch) -> None:
    class FakeHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            return [general_hospital(id=str(i), name="가상 병원", address="주소", phone="123", latitude=35, longitude=126) for i in range(10)]

        async def details(self, ykiho: str) -> HiraDetails:
            return HiraDetails()

    monkeypatch.setattr("app.recommend_api.HiraClient", FakeHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True, "include_audio": True}).json()
        assert result["status"] == "ok"
        assert result["audio_status"] == "unavailable"
        assert result["hospitals"]


def test_dense_area_over_capacity_reports_search_limit(tmp_path: Path, monkeypatch) -> None:
    class OverfullHira(FakeClientContext):
        def __init__(self, key: str) -> None:
            pass

        async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *, specialty_code: str | None = None, classification_code: str | None = None) -> list[HiraHospital]:
            raise HiraTooBroad("too many")

    monkeypatch.setattr("app.recommend_api.HiraClient", OverfullHira)
    with make_client(tmp_path, "fake") as client:
        call_id = call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert result["status"] == "unavailable"
        assert result["hospitals"] == []
        assert result["warning"]
