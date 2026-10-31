"""An urgent request takes priority over ordinary call processing."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from app.config import Settings
from app.call_store import CallStore
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS
from app.schemas import TurnInput


def test_urgent_button_before_consent_persists_without_caller_text(tmp_path: Path) -> None:
    path = tmp_path / "urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        result = client.post(f"/calls/{call_id}/urgent")
        assert result.status_code == 200
        assert result.json()["urgent"] is True
        assert result.json()["state"] == "awaiting_consent"
        assert client.get(f"/sessions/{call_id}/history").json() == []


def test_preconsent_emergency_cannot_be_followed_by_normal_consent(tmp_path: Path) -> None:
    path = tmp_path / "urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        result = client.post(f"/calls/{call_id}/consent", json={"text": "숨을 못 쉬어요"})
        assert result.status_code == 200
        assert result.json()["urgent"] is True
        assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 409
        assert client.get(f"/sessions/{call_id}/history").json() == []


def test_urgent_button_cancels_pending_and_blocks_new_turns(tmp_path: Path) -> None:
    path = tmp_path / "urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/location-consent", json={"accepted": True})
        db.create_turn(call_id, "pending", "기침이 나요")
        assert client.post(f"/calls/{call_id}/urgent").json()["urgent"] is True
        assert db.get_turn(call_id, "pending")["status"] == "cancelled"
        assert client.get(f"/calls/{call_id}/messages/pending").status_code == 409
        assert client.get(f"/calls/{call_id}/messages/pending/audio/low").status_code == 409
        assert client.get(f"/calls/{call_id}/prompts/question/audio").status_code == 409
        assert client.post(f"/calls/{call_id}/messages", json={"request_id": "next", "text": "기침"}).status_code == 409
        result = client.post(f"/calls/{call_id}/recommendations", json={"latitude": 35, "longitude": 126,
                             "location_consent": True, "urgent": False}).json()
        assert result["status"] == "urgent"
        assert result["hospitals"] == []


def test_split_emergency_marks_call_and_cancels_pending_turn(tmp_path: Path) -> None:
    path = tmp_path / "split-urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        db.create_turn(call_id, "first", "어제 농약을 뿌렸어요")
        response = client.post(f"/calls/{call_id}/messages", json={"request_id": "second", "text": "어지러워요"})
        assert response.status_code == 202
        assert response.json()["urgent"] is True
        assert db.get_turn(call_id, "first")["status"] == "cancelled"
        assert db.get_turn(call_id, "second")["status"] == "emergency"
        assert client.get(f"/calls/{call_id}/messages/first").status_code == 409


def test_emergency_context_is_not_shared_between_calls(tmp_path: Path) -> None:
    path = tmp_path / "separate-urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        first = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        second = client.post("/calls", json={"phone": "+821000000002"}).json()["id"]
        for call_id in (first, second):
            client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        db.create_turn(first, "first", "어제 농약을 뿌렸어요")
        response = client.post(f"/calls/{second}/messages", json={"request_id": "second", "text": "어지러워요"})
        assert response.status_code == 202
        assert response.json().get("urgent") is not True
        assert client.get(f"/calls/{second}/messages/second").status_code == 200


def test_split_pesticide_emergency_returns_specialized_voice_route(tmp_path: Path) -> None:
    path = tmp_path / "pesticide-urgent.sqlite"
    db = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        first = client.post(f"/calls/{call_id}/messages", json={"request_id": "first", "text": "어제 농약을 뿌렸어요"})
        second = client.post(f"/calls/{call_id}/messages", json={"request_id": "second", "text": "어지러워요"})
        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["urgent"] is True
        # This prefix is consumed by the frontend to choose the pesticide voice prompt.
        assert second.json()["final_text"].startswith("농약 때문에 위험할 수 있습니다.")


def test_concurrent_emergency_prevents_routine_turn_writes(tmp_path: Path) -> None:
    db = Database(tmp_path / "concurrent.sqlite")
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    session = db.create_session("+821000000001")
    call_id = str(session["id"])
    calls.create(call_id)
    calls.decide(call_id, "accepted")
    barrier = Barrier(2)

    def submit(request_id: str, text: str) -> None:
        barrier.wait()
        calls.submit_turn(call_id, TurnInput(request_id=request_id, text=text))

    with ThreadPoolExecutor(max_workers=2) as pool:
        normal = pool.submit(submit, "normal", "기침이 나요")
        urgent = pool.submit(submit, "urgent", "숨을 못 쉬어요")
        normal.result()
        urgent.result()
    assert calls.is_urgent(call_id)
    normal_turn = db.get_turn(call_id, "normal")
    if normal_turn is not None:
        assert normal_turn["status"] == "cancelled"
        assert not calls.update_turn_if_safe(str(normal_turn["id"]), {"final_text": "ordinary response"})
