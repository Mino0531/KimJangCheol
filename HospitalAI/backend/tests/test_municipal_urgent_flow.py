"""The municipal queue must preserve urgent notices only after new consent."""

import json
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2
import uvicorn
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def _events(database: Database) -> list[dict]:
    with database.connect() as con:
        return [json.loads(row[0]) for row in con.execute("SELECT body FROM municipal_outbox ORDER BY rowid")]


def test_urgent_event_is_enqueued_after_consent_without_waiting_for_model(tmp_path: Path) -> None:
    path = tmp_path / "urgent.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})

        response = client.post(f"/calls/{call_id}/messages", json={"request_id": "urgent", "text": "숨을 못 쉬어요"})

        assert response.json()["urgent"] is True
        events = _events(database)
        assert [event["event_type"] for event in events] == ["call_started", "urgent"]
        assert events[-1]["user_text"] == "숨을 못 쉬어요"


def test_urgent_before_consent_does_not_share_caller_data(tmp_path: Path) -> None:
    path = tmp_path / "preconsent.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]

        response = client.post(f"/calls/{call_id}/urgent")

        assert response.json()["urgent"] is True
        assert _events(database) == []


def test_urgent_after_consent_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})

        client.post(f"/calls/{call_id}/urgent")
        client.post(f"/calls/{call_id}/urgent")

        assert sum(event["event_type"] == "urgent" for event in _events(database)) == 1


def test_urgent_http_response_does_not_wait_for_outbox_storage(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "socket.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    entered, release = threading.Event(), threading.Event()

    def delayed_enqueue(self, call_id):
        entered.set()
        release.wait(10)
        return False

    monkeypatch.setattr(MunicipalOutbox, "enqueue_urgent", delayed_enqueue)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=3,
                           headers={"Authorization": "Bearer s"}) as client:
            call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
            client.post(f"/calls/{call_id}/consent", json={"text": "동의"})

            response = client.post(f"/calls/{call_id}/urgent")

            assert response.json()["urgent"] is True
            assert entered.wait(3)
            assert not release.is_set()
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
    assert not thread.is_alive()


def test_urgent_notice_recovers_after_temporary_queue_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "recovery.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    services = ServiceBundle(MockLLM(), MockTTS())
    app = create_app(settings, database, services)

    def unavailable(self, call_id):
        raise sqlite3.OperationalError("queue unavailable")

    with TestClient(app, headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        with monkeypatch.context() as scoped:
            scoped.setattr(MunicipalOutbox, "enqueue_urgent", unavailable)
            response = client.post(f"/calls/{call_id}/urgent")
        assert response.json()["urgent"] is True
        assert [event["event_type"] for event in _events(database)] == ["call_started"]

    create_app(settings, database, services)

    assert [event["event_type"] for event in _events(database)] == ["call_started", "urgent"]


def test_late_urgent_details_are_delivered_once_after_emergency_latch(tmp_path: Path) -> None:
    path = tmp_path / "late-details.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/urgent")

        response = client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})
        client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})

        assert response.json()["urgent"] is True
        details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}:")]
        assert len(details) == 1
        assert details[0]["user_text"] == "숨을 못 쉬어요"


def test_preconsent_urgent_text_is_never_stored_or_sent(tmp_path: Path) -> None:
    path = tmp_path / "preconsent-details.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]

        response = client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})

        assert response.json()["urgent"] is True
        assert _events(database) == []
        with database.connect() as con:
            assert con.execute("SELECT count(*) FROM urgent_details WHERE call_id=?", (call_id,)).fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM urgent_detail_events WHERE call_id=?", (call_id,)).fetchone()[0] == 0


def test_urgent_and_message_race_keeps_one_detail_event(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    headers = {"Authorization": "Bearer s"}
    barrier = threading.Barrier(2)
    with TestClient(app, headers=headers) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})

        def submit_urgent():
            barrier.wait()
            return client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})

        def submit_message():
            barrier.wait()
            return client.post(f"/calls/{call_id}/messages", json={"request_id": "urgent", "text": "숨을 못 쉬어요"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            urgent = pool.submit(submit_urgent)
            message = pool.submit(submit_message)
            assert urgent.result().status_code == 200
            assert message.result().status_code in (202, 409)

        details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}:")]
        assert len(details) == 1
        assert details[0]["user_text"] == "숨을 못 쉬어요"


def test_old_consent_never_permits_urgent_text_storage(tmp_path: Path) -> None:
    path = tmp_path / "old-consent.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        with database.connect() as con:
            con.execute("UPDATE calls SET state='accepted', consent_version='prototype-v1' WHERE id=?", (call_id,))
            con.commit()

        response = client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})

        assert response.json()["urgent"] is True
        with database.connect() as con:
            assert con.execute("SELECT count(*) FROM urgent_details WHERE call_id=?", (call_id,)).fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM urgent_detail_events WHERE call_id=?", (call_id,)).fetchone()[0] == 0
        assert _events(database) == []


def test_missing_urgent_detail_is_recovered_after_restart(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "detail-recovery.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    services = ServiceBundle(MockLLM(), MockTTS())
    app = create_app(settings, database, services)

    def unavailable(self, call_id):
        raise sqlite3.OperationalError("queue unavailable")

    with TestClient(app, headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        with monkeypatch.context() as scoped:
            scoped.setattr(MunicipalOutbox, "enqueue_urgent_details", unavailable)
            response = client.post(f"/calls/{call_id}/urgent", json={"text": "숨을 못 쉬어요"})
        assert response.json()["urgent"] is True
        assert not any(event["event_id"].startswith(f"urgent-detail:{call_id}:") for event in _events(database))

    create_app(settings, database, services)

    details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}:")]
    assert len(details) == 1
    assert details[0]["user_text"] == "숨을 못 쉬어요"


def test_distinct_late_urgent_details_are_all_sent_once(tmp_path: Path) -> None:
    path = tmp_path / "multiple-details.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/urgent", json={"text": "어지러워요"})

        client.post(f"/calls/{call_id}/urgent", json={"text": "농약을 뿌린 뒤 어지러워요"})
        client.post(f"/calls/{call_id}/urgent", json={"text": "어지러워요"})

        details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}:")]
        assert {event["user_text"] for event in details} == {"어지러워요", "농약을 뿌린 뒤 어지러워요"}
        assert len(details) == 2
        assert len({event["event_id"] for event in details}) == 2


def test_distinct_urgent_detail_submissions_are_atomic(tmp_path: Path) -> None:
    path = tmp_path / "details-race.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    barrier = threading.Barrier(2)
    with TestClient(app, headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})

        def submit(text):
            barrier.wait()
            return client.post(f"/calls/{call_id}/urgent", json={"text": text})

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit, "어지러워요")
            second = pool.submit(submit, "농약을 뿌렸어요")
            assert first.result().status_code == second.result().status_code == 200

        details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}:")]
        assert {event["user_text"] for event in details} == {"어지러워요", "농약을 뿌렸어요"}
        assert len(details) == 2


def test_legacy_urgent_detail_is_migrated_without_duplicate_notification(tmp_path: Path) -> None:
    path = tmp_path / "legacy-detail.sqlite"
    database = Database(path)
    settings = Settings(environment="development", service_token="s", database_path=str(path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer s"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        client.post(f"/calls/{call_id}/urgent", json={"text": "어지러워요"})
        with database.connect() as con:
            first = con.execute("SELECT body FROM municipal_outbox WHERE event_id LIKE ?", (f"urgent-detail:{call_id}:%",)).fetchone()
            event = json.loads(first[0])
            event["event_id"] = f"urgent-detail:{call_id}"
            con.execute("DELETE FROM municipal_outbox WHERE event_id LIKE ?", (f"urgent-detail:{call_id}:%",))
            con.execute("DELETE FROM urgent_detail_events WHERE call_id=?", (call_id,))
            con.execute("INSERT INTO municipal_outbox(event_id,call_id,event_type,body,state) VALUES(?,?,?,?,?)",
                        (event["event_id"], call_id, "urgent", json.dumps(event), "sent"))
            con.commit()

        CallStore(database).initialize()
        MunicipalOutbox(database).recover()
        client.post(f"/calls/{call_id}/urgent", json={"text": "농약을 뿌렸어요"})

        details = [event for event in _events(database) if event["event_id"].startswith(f"urgent-detail:{call_id}")]
        assert len(details) == 2
        assert {event["user_text"] for event in details} == {"어지러워요", "농약을 뿌렸어요"}
