"""A caller's latest concern is used only as a tentative department hint."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.call_specialty import select_call_specialty
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommendation import HospitalCandidate, RecommendationResult, recommend
from app.services import MockLLM, MockTTS


def test_unrelated_negative_does_not_erase_previous_knee_symptom() -> None:
    history = [{"input_text": "무릎이 아파요"}, {"input_text": "열은 없어요"}]
    assert select_call_specialty(None, history).specialty == "정형외과"


def test_denied_shortness_of_breath_does_not_erase_previous_cough() -> None:
    history = [{"input_text": "기침이 나요"}, {"input_text": "숨은 차지 않아요"}]
    assert select_call_specialty(None, history).specialty == "내과"


def test_later_denial_of_original_symptom_does_not_keep_old_hint() -> None:
    history = [{"input_text": "무릎이 아파요"}, {"input_text": "무릎은 안 아파요"}]
    assert select_call_specialty(None, history).specialty is None


def test_unqualified_negative_reply_does_not_infer_a_specialty() -> None:
    history = [{"input_text": "기침이 나요"}, {"input_text": "아니요"}]
    assert select_call_specialty(None, history).specialty is None


def test_mixed_positive_symptoms_do_not_pick_a_single_specialty() -> None:
    history = [{"input_text": "기침이 나요"}, {"input_text": "무릎이 아파요"}]
    assert select_call_specialty(None, history).specialty is None


def test_previous_denial_then_positive_symptom_remains_uncertain() -> None:
    history = [{"input_text": "기침은 없어요"}, {"input_text": "기침이 나요"}]
    assert select_call_specialty(None, history).specialty is None


def test_unqualified_affirmation_does_not_confirm_a_specialty() -> None:
    history = [{"input_text": "기침이 나요"}, {"input_text": "네"}]
    assert select_call_specialty(None, history).specialty is None


def test_generic_offer_retains_clear_symptom_in_the_same_turn() -> None:
    history = [{"input_text": "갑자기 무릎이 아파요", "status": "degraded", "final_text": "병원을 찾아드릴까요?",
                "guidance_json": '{"action":"hospital_search_offer","question_id":null,"specialty":null}',
                "guidance_delivered": 1}]
    assert select_call_specialty(None, history).specialty == "정형외과"


def test_emergency_can_be_detected_across_turns() -> None:
    history = [{"input_text": "어제 농약을 뿌렸어요"}, {"input_text": "어지러워요"}]
    assert select_call_specialty(None, history).urgent is True


def _call(client: TestClient) -> str:
    call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
    client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
    return call_id


def test_latest_concern_becomes_tentative_specialty(tmp_path: Path, monkeypatch) -> None:
    observed = []

    async def find(query, service_key):
        observed.append(query.specialty)
        return RecommendationResult(status="ok")

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q", "text": "기침이 나요"})
        result = client.post(f"/calls/{call_id}/recommendations",
                             json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert observed == [None]
        assert result["specialty"] is None
        assert result["specialty_source"] == "unknown"


def test_region_reference_uses_region_coordinates_instead_of_stale_gps(tmp_path: Path, monkeypatch) -> None:
    observed = []

    async def find(query, service_key):
        observed.append((query.latitude, query.longitude))
        return RecommendationResult(status="unavailable")

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 37.65, "longitude": 126.77,
                             "location_consent": True, "location_precision": "region_reference",
                             "region_label": "화순군 백아면"})
        assert result.status_code == 200
        assert 35.1 < observed[0][0] < 35.2
        assert 127.1 < observed[0][1] < 127.2


def test_production_does_not_search_with_unconfirmed_device_location(tmp_path: Path, monkeypatch) -> None:
    observed = []

    async def find(query, service_key):
        observed.append(query)
        return RecommendationResult(status="ok")

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="production", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 37.65, "longitude": 126.77,
                             "location_consent": True})
        assert result.status_code == 409
        assert observed == []


def test_leg_pain_does_not_recommend_dental_clinic_when_specialty_is_unknown(tmp_path: Path, monkeypatch) -> None:
    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id="dental", name="연세플라워치과병원", latitude=35,
                                                   longitude=126, source="test")])

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q", "text": "다리가 지끈지끈해요"})
        result = client.post(f"/calls/{call_id}/recommendations",
                             json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert result["status"] == "unavailable"
        assert result["hospitals"] == []


def test_limb_pain_prefers_outpatient_facility_over_nursing_hospital(tmp_path: Path, monkeypatch) -> None:
    async def find(query, service_key):
        return recommend(query, [
            HospitalCandidate(id="nursing", name="가상요양병원", latitude=35, longitude=126,
                              specialties=("정형외과",), source="test"),
            HospitalCandidate(id="clinic", name="가상의원", latitude=35.01, longitude=126,
                              specialties=("정형외과",), source="test"),
        ])

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q", "text": "갑자기 무릎이 아파요"})
        result = client.post(f"/calls/{call_id}/recommendations",
                             json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert result["specialty"] is None
        assert result["hospitals"][0]["name"] == "가상의원"


def test_explicit_specialty_overrides_tentative_suggestion(tmp_path: Path, monkeypatch) -> None:
    observed = []

    async def find(query, service_key):
        observed.append(query.specialty)
        return RecommendationResult(status="ok")

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q", "text": "기침이 나요"})
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126,
                             "location_consent": True, "specialty": "이비인후과"}).json()
        assert observed == [None]
        assert result["specialty"] is None
        assert result["specialty_source"] == "unknown"


def test_later_answer_keeps_original_symptom_for_tentative_department(tmp_path: Path, monkeypatch) -> None:
    observed = []

    async def find(query, service_key):
        observed.append(query.specialty)
        return RecommendationResult(status="ok")

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q1", "text": "기침이 나요"})
        client.post(f"/calls/{call_id}/messages", json={"request_id": "q2", "text": "어제부터요"})
        result = client.post(f"/calls/{call_id}/recommendations",
                             json={"latitude": 35, "longitude": 126, "location_consent": True}).json()
        assert observed == [None]
        assert result["specialty_source"] == "unknown"


def test_chest_pain_takes_priority_over_department_selection(tmp_path: Path) -> None:
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "test.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = _call(client)
        turn = client.post(f"/calls/{call_id}/messages", json={"request_id": "q", "text": "가슴이 아프고 기침해요"}).json()
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126,
                             "location_consent": True, "specialty": "내과"}).json()
        assert turn["status"] == "emergency"
        assert turn["urgent"] is True
        assert client.get(f"/calls/{call_id}/messages/q").status_code == 409
        assert result["status"] == "urgent"
