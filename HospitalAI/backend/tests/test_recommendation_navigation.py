"""Same-call recommendation navigation and explicit department continuity."""

from pathlib import Path
from contextlib import closing

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommendation import HospitalCandidate, recommend
from app.services import MockLLM, MockTTS, TTSResult


def test_next_repeat_and_explicit_department_survive_short_replies(tmp_path: Path, monkeypatch) -> None:
    observed: list[str | None] = []

    async def find(query, service_key):
        observed.append(query.specialty)
        candidates = [HospitalCandidate(id=identity, name=name, latitude=35 + delta, longitude=126,
                                        specialties=("정형외과",), source="test")
                      for identity, name, delta in (("one", "첫 병원", 0.001), ("two", "다음 병원", 0.002))]
        return recommend(query, candidates)

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "navigation.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True, "specialty": "정형외과"}
        first = client.post(f"/calls/{call_id}/recommendations", json=body)
        assert first.status_code == 200
        assert first.json()["hospitals"][0]["id"] == "one"
        next_body = {**body, "specialty": None, "mode": "next"}
        second = client.post(f"/calls/{call_id}/recommendations", json=next_body)
        assert second.status_code == 200
        assert second.json()["hospitals"][0]["id"] == "two"
        assert second.json()["specialty"] == "정형외과"
        repeated = client.post(f"/calls/{call_id}/recommendations", json={**next_body, "mode": "repeat"})
        assert repeated.status_code == 200
        assert repeated.json()["hospitals"][0]["id"] == "two"
        exhausted = client.post(f"/calls/{call_id}/recommendations", json=next_body)
        assert exhausted.status_code == 200
        assert exhausted.json()["status"] == "unavailable"
        assert observed == ["정형외과"] * 3


def test_location_withdrawal_does_not_restore_prior_hospital(tmp_path: Path, monkeypatch) -> None:
    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id="one", name="병원", latitude=35, longitude=126,
                                                    source="test")])

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "revoke.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True}
        assert client.post(f"/calls/{call_id}/recommendations", json=body).json()["status"] == "ok"
        revoked = client.post(f"/calls/{call_id}/location-consent", json={"accepted": False})
        assert revoked.status_code == 200
        with closing(Database(settings.database_path).connect()) as con:
            stored = con.execute("SELECT current_json,seen_json FROM call_recommendation_navigation WHERE call_id=?", (call_id,)).fetchone()
        assert stored["current_json"] is None and stored["seen_json"] == "[]"
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": True, "source_location_generation": 0,
            "source_location_revision": revoked.json()["location_revision"]}).status_code == 200
        result = client.post(f"/calls/{call_id}/recommendations", json={**body, "mode": "repeat", "source_location_generation": 2})
        assert result.status_code == 200
        assert result.json()["status"] == "unavailable"


def test_unplayed_recommendation_does_not_advance_and_ack_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id=identity, name=name, latitude=35 + delta, longitude=126,
                                                   source="test")
                                 for identity, name, delta in (("one", "첫 병원", 0.001), ("two", "다음 병원", 0.002))])

    class AudioTTS:
        async def deliver(self, text, kind):
            return TTSResult(status="synthesized", kind=kind, text=text, audio=b"audio", mime_type="audio/mpeg", interruptible=False)

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "ack.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), AudioTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True,
                "include_audio": True, "defer_confirmation": True}
        first = client.post(f"/calls/{call_id}/recommendations", json=body).json()
        assert first["hospitals"][0]["id"] == "one"
        with closing(Database(settings.database_path).connect()) as con:
            assert con.execute("SELECT count(*) FROM recommendation_outbox").fetchone()[0] == 0
        replacement = client.post(f"/calls/{call_id}/recommendations", json={**body, "mode": "next"}).json()
        assert replacement["hospitals"][0]["id"] == "one"
        endpoint = f"/calls/{call_id}/recommendations/confirm"
        ack_body = {"navigation_token": replacement["navigation_token"]}
        assert client.post(endpoint, json=ack_body).status_code == 200
        assert client.post(endpoint, json=ack_body).status_code == 200
        with closing(Database(settings.database_path).connect()) as con:
            assert con.execute("SELECT count(*) FROM recommendation_outbox").fetchone()[0] == 1
        assert client.post(endpoint, json={"navigation_token": first["navigation_token"]}).status_code == 409
        after = client.post(f"/calls/{call_id}/recommendations", json={**body, "mode": "next"}).json()
        assert after["hospitals"][0]["id"] == "two"
        assert "다음 병원" in after["speech_text"]


def test_location_context_reset_invalidates_pending_without_requiring_new_consent(tmp_path: Path, monkeypatch) -> None:
    from app.call_interpret import _Decision

    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id="one", name="병원", latitude=35, longitude=126,
                                                    source="test")])

    async def interpret(*args, **kwargs):
        return _Decision(intent="consultation", location_subject="other", patient_changed=True)

    class AudioTTS:
        async def deliver(self, text, kind):
            return TTSResult(status="synthesized", kind=kind, text=text, audio=b"audio", mime_type="audio/mpeg", interruptible=False)

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    monkeypatch.setattr("app.call_api.classify_decision", interpret)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "reset.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), AudioTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True, "include_audio": True}
        pending = client.post(f"/calls/{call_id}/recommendations", json=body).json()
        endpoint = f"/calls/{call_id}/location-context/reset"
        decision = client.post(f"/calls/{call_id}/interpret", json={"request_id": "other-patient", "text": "어머니가 아프세요"})
        assert decision.status_code == 200 and decision.json()["patient_changed"] is True
        assert client.post(endpoint, json={"request_id": "other-patient", "source_location_generation": 0}).status_code == 409
        reset = client.post(endpoint, json={"request_id": "other-patient", "source_location_generation": 1})
        assert reset.status_code == 200
        assert client.post(endpoint, json={"request_id": "other-patient", "source_location_generation": 1}).json() == reset.json()
        assert client.post(f"/calls/{call_id}/recommendations/confirm", json={"navigation_token": pending["navigation_token"]}).status_code == 404
        assert client.post(f"/calls/{call_id}/recommendations", json={**body, "source_location_generation": reset.json()["generation"]}).status_code == 200


def test_delayed_old_coordinates_are_rejected_before_external_lookup(tmp_path: Path, monkeypatch) -> None:
    calls = []

    async def find(query, service_key):
        calls.append(query.latitude)
        return recommend(query, [])

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "stale-coordinates.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        endpoint = f"/calls/{call_id}/recommendations"
        body = {"latitude": 35, "longitude": 126, "location_consent": True}
        revoked = client.post(f"/calls/{call_id}/location-consent", json={"accepted": False})
        assert revoked.status_code == 200
        assert client.post(f"/calls/{call_id}/location-consent", json={"accepted": True, "source_location_generation": 0,
            "source_location_revision": revoked.json()["location_revision"]}).status_code == 200
        assert client.post(endpoint, json={**body, "source_location_generation": 1}).status_code == 409
        assert client.post(endpoint, json=body).status_code == 409
        assert calls == []
        assert client.post(endpoint, json={**body, "source_location_generation": 2}).status_code == 200
        assert calls == [35]


def test_late_location_reaccept_does_not_override_withdrawal(tmp_path: Path) -> None:
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "late-consent.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        endpoint = f"/calls/{call_id}/location-consent"
        revoked = client.post(endpoint, json={"accepted": False, "source_location_generation": 1})
        assert revoked.status_code == 200
        assert client.post(endpoint, json={"accepted": True, "source_location_generation": 1}).status_code == 409
        assert client.post(endpoint, json={"accepted": True}).status_code == 409
        assert client.post(endpoint, json={"accepted": True, "source_location_generation": 0,
                                         "source_location_revision": revoked.json()["location_revision"]}).status_code == 200


def test_stale_zero_generation_reaccept_is_rejected_after_second_withdrawal(tmp_path: Path) -> None:
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "old-zero.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        endpoint = f"/calls/{call_id}/location-consent"
        first = client.post(endpoint, json={"accepted": False}).json()
        accepted = client.post(endpoint, json={"accepted": True, "source_location_generation": 0,
                                              "source_location_revision": first["location_revision"]})
        assert accepted.status_code == 200
        second = client.post(endpoint, json={"accepted": False}).json()
        assert client.post(endpoint, json={"accepted": True, "source_location_generation": 0,
                                          "source_location_revision": first["location_revision"]}).status_code == 409
        assert client.post(endpoint, json={"accepted": True, "source_location_generation": 0}).status_code == 409
        assert client.post(endpoint, json={"accepted": True, "source_location_generation": 0,
                                          "source_location_revision": second["location_revision"]}).status_code == 200


def test_subject_switch_does_not_infer_department_from_previous_patient(tmp_path: Path, monkeypatch) -> None:
    from app.call_interpret import _Decision

    selected = []

    async def find(query, service_key):
        selected.append(query.specialty)
        return recommend(query, [])

    async def interpret(*args, **kwargs):
        return _Decision(intent="consultation", location_subject="other", patient_changed=True)

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    monkeypatch.setattr("app.call_api.classify_decision", interpret)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "subject.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/messages", json={"request_id": "self", "text": "기침이 나요"})
        response = client.post(f"/calls/{call_id}/interpret", json={"request_id": "other", "text": "이제 어머니 병원 부탁해요"})
        assert response.status_code == 200
        reset = client.post(f"/calls/{call_id}/location-context/reset", json={"request_id": "other"})
        assert reset.status_code == 200
        response = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126,
            "location_consent": True, "source_location_generation": reset.json()["generation"]})
        assert response.status_code == 200
        assert selected == [None]


def test_next_preserves_tentative_department_even_after_short_affirmation(tmp_path: Path, monkeypatch) -> None:
    observed: list[str | None] = []

    async def find(query, service_key):
        observed.append(query.specialty)
        return recommend(query, [HospitalCandidate(id=identity, name=identity, latitude=35 + delta, longitude=126,
                                                    specialties=("내과",), source="test")
                                 for identity, delta in (("one", .001), ("two", .002))])

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "inferred.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/messages", json={"request_id": "cough", "text": "기침이 나요"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True}
        assert client.post(f"/calls/{call_id}/recommendations", json=body).json()["specialty_source"] == "suggested"
        client.post(f"/calls/{call_id}/messages", json={"request_id": "yes", "text": "네"})
        other = client.post(f"/calls/{call_id}/recommendations", json={**body, "mode": "next"}).json()
        assert other["specialty"] == "내과"
        assert other["specialty_source"] == "suggested"
        assert observed == ["내과", "내과"]


def test_region_approximation_is_spoken_and_unplayed_result_is_not_saved(tmp_path: Path, monkeypatch) -> None:
    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id="one", name="병원", latitude=35, longitude=126,
                                                    source="test")])

    class AudioTTS:
        async def deliver(self, text, kind):
            return TTSResult(status="synthesized", kind=kind, text=text, audio=b"audio", mime_type="audio/mpeg", interruptible=False)

    monkeypatch.setattr("app.call_api.public_recommendations", find)
    settings = Settings(environment="development", service_token="s", database_path=str(tmp_path / "region.sqlite"))
    with TestClient(create_app(settings, Database(settings.database_path), ServiceBundle(MockLLM(), AudioTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        body = {"latitude": 35, "longitude": 126, "location_consent": True, "include_audio": True,
                "location_precision": "region_reference", "region_label": "화순읍"}
        result = client.post(f"/calls/{call_id}/recommendations", json=body).json()
        assert "화순읍" in result["speech_text"] and "기준" in result["speech_text"]
        assert "대략적인 거리" in result["warning"]
        with closing(Database(settings.database_path).connect()) as con:
            assert con.execute("SELECT count(*) FROM recommendation_outbox").fetchone()[0] == 0
