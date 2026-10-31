"""Loopback end-to-end checks against the municipal and hospital inboxes."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS

ROOT = Path(__file__).resolve().parents[2]
HOSPITAL = ROOT / "external" / "KimJangCheol"
PYTHON = HOSPITAL / "backend" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
TOKEN = "synthetic-only-test-token"
INTAKE_TOKEN = "synthetic-intake-token"
STAFF_TOKEN = "synthetic-staff-token"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def hospital_server(tmp_path: Path, port: int) -> Iterator[str]:
    if not PYTHON.exists():
        pytest.skip("Install hospital backend/.venv to run real-receiver integration")
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}}
    environment["HOSPITAL_DB_PATH"] = str(tmp_path / "hospital-receiver.sqlite3")
    environment["HOSPITAL_DASHBOARD_TOKEN"] = TOKEN
    environment["MUNICIPAL_INTAKE_TOKEN"] = INTAKE_TOKEN
    environment["MUNICIPAL_STAFF_TOKEN"] = STAFF_TOKEN
    process = subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=HOSPITAL, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx2.Client(timeout=1, trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get(url + "/api/health").status_code == 200:
                        break
                except (httpx2.ConnectError, httpx2.ConnectTimeout):
                    Event().wait(0.1)
            else:
                pytest.fail("Hospital receiver did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@contextmanager
def phone_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> Iterator[TestClient]:
    monkeypatch.setenv("HOSPITAL_DASHBOARD_URL", url)
    monkeypatch.setenv("HOSPITAL_DASHBOARD_TOKEN", TOKEN)
    path = tmp_path / "phone.sqlite3"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        seed_demo=True, database_path=str(path), hospital_url=url, hospital_token=TOKEN,
                        municipal_url=url, municipal_token=INTAKE_TOKEN)
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        yield client


def hospital_rows(url: str, token: str = TOKEN) -> list[dict]:
    with httpx2.Client(timeout=3, trust_env=False) as client:
        response = client.get(url + "/api/team/consultation-results", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.json()


def test_consented_call_requires_staff_review_before_hospital_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    with hospital_server(tmp_path, port) as url, phone_client(tmp_path, monkeypatch, url) as client:
        with httpx2.Client(timeout=3, trust_env=False) as unauthenticated:
            assert unauthenticated.get(url + "/api/team/consultation-results").status_code == 401
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        body = {"request_id": "case-1", "text": "기침이 있어요"}
        assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 403
        assert hospital_rows(url) == []
        assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).json()["state"] == "accepted"
        assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 202
        assert client.post(f"/calls/{call_id}/messages", json=body).status_code == 202
        assert hospital_rows(url) == []
        with httpx2.Client(timeout=3, trust_env=False, headers={"Authorization": f"Bearer {STAFF_TOKEN}"}) as staff:
            case = staff.get(f"{url}/api/municipality/inbox/{call_id}").json()
            assert [event["event_type"] for event in case["events"]] == ["call_started", "turn_completed"]
            assert case["events"][1]["user_text"] == body["text"]
            assert staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                              json={"expected_version": case["version"]}).status_code == 409
            acknowledged = staff.post(f"{url}/api/municipality/inbox/{call_id}/ack",
                                      json={"expected_version": case["version"]}).json()["case"]
            handoff = staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                                 json={"expected_version": acknowledged["version"]})
            assert handoff.status_code == 200
            assert staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                              json={"expected_version": acknowledged["version"]}).json()["duplicate"] is True
        rows = hospital_rows(url)
        assert len(rows) == 1 and rows[0]["call_id"] == call_id
        assert rows[0]["user_text"] == body["text"] and rows[0]["assistant_text"]
        assert not {"name", "age", "region"}.intersection(rows[0])


def test_offline_municipality_retries_original_events_without_hospital_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    with phone_client(tmp_path, monkeypatch, url) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"})
        response = client.post(f"/calls/{call_id}/messages", json={"request_id": "case-1", "text": "기침이 있어요"})
        assert response.status_code == 202
        with closing(Database(tmp_path / "phone.sqlite3").connect()) as database:
            queued = database.execute("SELECT event_type, state FROM municipal_outbox WHERE call_id=? ORDER BY rowid", (call_id,)).fetchall()
        assert [(row["event_type"], row["state"]) for row in queued] == [
            ("call_started", "pending"), ("turn_completed", "pending")]
        with hospital_server(tmp_path, port):
            first = client.post("/calls/municipality/retry").json()
            second = client.post("/calls/municipality/retry").json()
            rows = hospital_rows(url)
            with httpx2.Client(timeout=3, trust_env=False, headers={"Authorization": f"Bearer {STAFF_TOKEN}"}) as staff:
                case = staff.get(f"{url}/api/municipality/inbox/{call_id}").json()
        # The background retry loop may deliver some events before the explicit retry.
        assert first["pending"] == 0
        assert second == {"delivered": 0, "pending": 0}
        assert [event["event_type"] for event in case["events"]] == ["call_started", "turn_completed"]
        assert rows == []
        assert client.post("/calls/hospital/retry").status_code == 409
