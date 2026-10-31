"""An urgent notification must not erase completed offline consultation history."""

import json
from pathlib import Path

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS
from test_dashboard_end_to_end import INTAKE_TOKEN, STAFF_TOKEN, free_port, hospital_rows, hospital_server


def test_urgent_delivery_precedes_offline_history_and_reopens_review(tmp_path: Path) -> None:
    """Late completed history remains visible and requires a new staff review."""
    database = Database(tmp_path / "phone.sqlite3")
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        seed_demo=True, database_path=database.path)
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as caller:
        call_id = caller.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        assert caller.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
        old_text = "어제부터 기침이 있습니다."
        assert caller.post(f"/calls/{call_id}/messages", json={"request_id": "prior", "text": old_text}).status_code == 202
        assert caller.post(f"/calls/{call_id}/urgent", json={}).status_code == 200
        assert caller.post(f"/calls/{call_id}/messages", json={"request_id": "later", "text": "다른 문의"}).status_code == 409

        outbox = MunicipalOutbox(database)
        pending = outbox._next()
        assert pending is not None
        event_id, queued_call_id, body = pending
        assert json.loads(body)["event_type"] == "urgent"
        assert queued_call_id == call_id
        with hospital_server(tmp_path, free_port()) as url:
            with httpx2.Client(timeout=3, trust_env=False) as intake:
                response = intake.post(f"{url}/api/municipality/intake", content=body.encode(),
                    headers={"Authorization": f"Bearer {INTAKE_TOKEN}", "Content-Type": "application/json"})
            assert response.status_code == 201
            outbox._mark_sent(event_id)
            with httpx2.Client(timeout=3, trust_env=False, headers={"Authorization": f"Bearer {STAFF_TOKEN}"}) as staff:
                first = staff.get(f"{url}/api/municipality/inbox/{call_id}").json()
                assert first["urgent"] and first["unread"]
                read = staff.post(f"{url}/api/municipality/inbox/{call_id}/ack",
                    json={"expected_version": first["version"]}).json()["case"]
                assert staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                    json={"expected_version": read["version"]}).status_code == 200
                result = anyio.run(outbox.dispatch, MunicipalConfig(url, INTAKE_TOKEN))
                assert result.pending == 0
                latest = staff.get(f"{url}/api/municipality/inbox/{call_id}").json()
                assert latest["urgent"] and latest["unread"]
                assert any(e["event_type"] == "turn_completed" and e["user_text"] == old_text for e in latest["events"])
                assert staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                    json={"expected_version": latest["version"]}).status_code == 409
                latest = staff.post(f"{url}/api/municipality/inbox/{call_id}/ack",
                    json={"expected_version": latest["version"]}).json()["case"]
                assert staff.post(f"{url}/api/municipality/inbox/{call_id}/handoff",
                    json={"expected_version": latest["version"]}).status_code == 200
            assert any(row["user_text"] == old_text for row in hospital_rows(url))
