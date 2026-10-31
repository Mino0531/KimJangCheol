"""Consent-to-municipality delivery tests using SQLite and a local HTTP receiver."""

from __future__ import annotations

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def test_call_forwards_to_municipality_only_after_consent(tmp_path: Path) -> None:
    received: list[tuple[str, str | None, bytes]] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            raw = self.rfile.read(length)
            received.append((self.path, self.headers.get("Authorization"), raw))
            event = json.loads(raw)
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"applied": True, "duplicate": False,
                "event_id": event["event_id"], "call_id": event["call_id"]}).encode())

        def log_message(self, format: str, *args: str) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "calls.sqlite3"
    settings = Settings(environment="development", service_token="service-token", admin_token="admin-token",
                        database_path=str(path), seed_demo=True,
                        hospital_url=f"http://127.0.0.1:{server.server_port}", hospital_token="hospital-token",
                        municipal_url=f"http://127.0.0.1:{server.server_port}", municipal_token="municipal-token")
    try:
        app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
        with TestClient(app, headers={"Authorization": "Bearer service-token"}) as client:
            created = client.post("/calls", json={"phone": "+821000000001"})
            assert created.status_code == 201
            call_id = created.json()["id"]
            body = {"request_id": "synthetic-1", "text": "기침이 있어요"}
            assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 403
            assert received == []
            assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).json()["state"] == "accepted"
            assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 202
            assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 202
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(received) == 2
    assert all(path == "/api/municipality/intake" and auth == "Bearer municipal-token" for path, auth, _ in received)
    events = [json.loads(raw) for _, _, raw in received]
    assert [event["event_type"] for event in events] == ["call_started", "turn_completed"]
    event = events[1]
    assert event["source"] == "hospital-ai"
    assert event["call_id"] == call_id
    assert event["user_text"] == body["text"]
    assert event["answer_status"] == "completed"
    assert event["event_id"].startswith("turn:")
    assert event["assistant_text"]


def test_disabled_bridge_stores_consented_events_for_future_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSPITAL_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HOSPITAL_DASHBOARD_TOKEN", "ignored-ambient-token")
    path = tmp_path / "calls.sqlite3"
    settings = Settings(environment="development", service_token="service-token", admin_token="admin-token",
                        database_path=str(path), seed_demo=True)
    db = Database(path)
    app = create_app(settings, db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service-token"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        response = client.post(f"/calls/{call_id}/messages", json={"request_id": "synthetic-1", "text": "기침이 있어요"})
        assert response.status_code == 202
    with closing(db.connect()) as con:
        events = con.execute("SELECT event_type, state FROM municipal_outbox WHERE call_id = ? ORDER BY rowid", (call_id,)).fetchall()
    assert [(event["event_type"], event["state"]) for event in events] == [
        ("call_started", "pending"), ("turn_completed", "pending")]
