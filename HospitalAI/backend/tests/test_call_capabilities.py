"""Local patient-call capabilities never grant shared service access."""

from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def make_client(tmp_path: Path, environment: str = "development") -> TestClient:
    db_path = tmp_path / "capabilities.sqlite3"
    settings = Settings(environment=environment, service_token="service", admin_token="admin",
                        database_path=str(db_path), dev_verification_token="verify" if environment == "development" else "")
    app = create_app(settings, Database(db_path), ServiceBundle(MockLLM(), MockTTS()))
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234))


def create_public_call(client: TestClient) -> dict:
    response = client.post("/calls", json={"phone": "+821000000001"}, headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def test_local_same_origin_patient_uses_only_own_call(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first = create_public_call(client)
        second = create_public_call(client)
        headers = {"Authorization": f"Bearer {first['call_token']}"}
        assert client.get(f"/calls/{first['id']}", headers=headers).status_code == 200
        assert client.post(f"/calls/{first['id']}/consent", json={"text": "동의"}, headers=headers).status_code == 200
        assert client.get(f"/calls/{second['id']}", headers=headers).status_code == 401
        assert client.get("/calls", headers=headers).status_code == 401
        assert client.post("/calls/hospital/retry", headers=headers).status_code == 401
        assert client.get(f"/sessions/{first['id']}/history", headers=headers).status_code == 401


def test_cross_origin_and_remote_creation_are_denied(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.post("/calls", json={"phone": "+821000000001"}, headers={"Origin": "https://attacker.example"}).status_code in (401, 403)
        assert client.post("/calls", json={"phone": "+821000000001"}).status_code in (401, 403)


def test_non_loopback_peer_cannot_create_call(tmp_path: Path) -> None:
    app = make_client(tmp_path).app
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("203.0.113.10", 1234)) as client:
        response = client.post("/calls", json={"phone": "+821000000001"}, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code in (401, 403)


def test_public_creation_cannot_claim_patient_verification(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/calls", json={"phone": "+821000000001", "verification_token": "verify"},
                               headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code in (401, 403)


def test_public_creation_is_not_enabled_in_production(tmp_path: Path) -> None:
    with make_client(tmp_path, "production") as client:
        response = client.post("/calls", json={"phone": "+821000000001"}, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code in (401, 403)


def test_existing_service_bearer_keeps_full_integration_access(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/calls", json={"phone": "+821000000001"}, headers={"Authorization": "Bearer service"})
        assert response.status_code == 201
        assert "call_token" not in response.json()
        assert client.get("/calls", headers={"Authorization": "Bearer service"}).status_code == 200


def test_capability_is_hashed_in_database_and_expires(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call = create_public_call(client)
        with sqlite3.connect(str(tmp_path / "capabilities.sqlite3")) as con:
            row = con.execute("SELECT token_hash FROM call_capabilities WHERE call_id = ?", (call["id"],)).fetchone()
            assert row is not None and row[0] != call["call_token"]
            con.execute("UPDATE call_capabilities SET expires_at = 0 WHERE call_id = ?", (call["id"],))
        response = client.get(f"/calls/{call['id']}", headers={"Authorization": f"Bearer {call['call_token']}"})
        assert response.status_code == 401


def test_rate_limit_prevents_session_creation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        for _ in range(20):
            create_public_call(client)
        response = client.post("/calls", json={"phone": "+821000000001"}, headers={"Origin": "http://127.0.0.1:8000"})
        assert response.status_code == 429
        with sqlite3.connect(str(tmp_path / "capabilities.sqlite3")) as con:
            assert con.execute("SELECT count(*) FROM calls").fetchone()[0] == 20


def test_refresh_issues_new_scoped_token_and_preserves_inflight_token(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call = create_public_call(client)
        headers = {"Authorization": f"Bearer {call['call_token']}"}
        response = client.post(f"/calls/{call['id']}/capability/refresh", headers=headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        refreshed = response.json()["call_token"]
        assert refreshed != call["call_token"]
        assert response.json()["expires_in"] == 3600
        assert client.get(f"/calls/{call['id']}", headers={"Authorization": f"Bearer {refreshed}"}).status_code == 200
        assert client.get(f"/calls/{call['id']}", headers=headers).status_code == 200


def test_refresh_rejects_expired_and_foreign_tokens(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        first, second = create_public_call(client), create_public_call(client)
        headers = {"Authorization": f"Bearer {first['call_token']}"}
        assert client.post(f"/calls/{second['id']}/capability/refresh", headers=headers).status_code == 401
        with sqlite3.connect(str(tmp_path / "capabilities.sqlite3")) as con:
            con.execute("UPDATE call_capabilities SET expires_at = 0 WHERE call_id = ?", (first["id"],))
        assert client.post(f"/calls/{first['id']}/capability/refresh", headers=headers).status_code == 401


def test_refresh_rejects_ended_and_revoked_calls(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ended, revoked = create_public_call(client), create_public_call(client)
        end_headers = {"Authorization": f"Bearer {ended['call_token']}"}
        revoke_headers = {"Authorization": f"Bearer {revoked['call_token']}"}
        assert client.post(f"/calls/{ended['id']}/end", json={"reason": "caller"}, headers=end_headers).status_code == 200
        assert client.post(f"/calls/{revoked['id']}/consent", json={"text": "동의"}, headers=revoke_headers).status_code == 200
        assert client.post(f"/calls/{revoked['id']}/revoke", headers=revoke_headers).status_code == 200
        assert client.post(f"/calls/{ended['id']}/capability/refresh", headers=end_headers).status_code == 409
        assert client.post(f"/calls/{revoked['id']}/capability/refresh", headers=revoke_headers).status_code == 409


def test_refresh_rejects_service_token(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call = create_public_call(client)
        response = client.post(f"/calls/{call['id']}/capability/refresh", headers={"Authorization": "Bearer service"})
        assert response.status_code == 401
