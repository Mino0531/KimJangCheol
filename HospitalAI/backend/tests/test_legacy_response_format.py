"""Older structured responses are not displayed, spoken or forwarded raw."""

import json
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.hospital_bridge import HospitalConfig, HospitalOutbox
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def legacy_app(tmp_path: Path) -> tuple[TestClient, Database, str, str]:
    db = Database(tmp_path / "legacy.sqlite")
    settings = Settings(environment="development", service_token="local", database_path=str(db.path))
    app = create_app(settings, db, ServiceBundle(MockLLM(), MockTTS()))
    client = TestClient(app, headers={"Authorization": "Bearer local"})
    call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
    client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).raise_for_status()
    turn, _ = db.create_turn(call_id, "legacy", "기침이 있어요")
    raw = json.dumps({"display_text": "이전 화면 답변", "tts_text": "이전 음성 답변", "internal": "sensitive-hidden"}, ensure_ascii=False)
    db.update_turn(str(turn["id"]), {"status": "completed", "final_text": raw, "sources_json": "[]"})
    return client, db, call_id, str(turn["id"])


def test_call_read_normalizes_legacy_json_without_altering_storage(tmp_path: Path) -> None:
    client, db, call_id, _ = legacy_app(tmp_path)
    with client:
        response = client.get(f"/calls/{call_id}/messages/legacy")
    assert response.json()["final_text"] == "이전 화면 답변"
    assert "sensitive-hidden" not in response.text
    assert "sensitive-hidden" in str(db.get_turn(call_id, "legacy")["final_text"])


def test_legacy_audio_is_not_exposed_when_it_may_contain_raw_json(tmp_path: Path) -> None:
    from app.audio_store import Audio, AudioStore
    client, db, call_id, turn_id = legacy_app(tmp_path)
    AudioStore(db).save(turn_id, "final", Audio(b"raw-json-audio", "audio/mpeg"))
    with client:
        response = client.get(f"/calls/{call_id}/messages/legacy/audio/final")
        legacy_route = client.get(f"/sessions/{call_id}/turns/legacy/audio/final")
    assert response.status_code == 409
    assert legacy_route.status_code == 409
    assert b"raw-json-audio" not in response.content


def test_double_encoded_legacy_json_cannot_be_shown_or_replayed(tmp_path: Path) -> None:
    from app.audio_store import Audio, AudioStore
    client, db, call_id, turn_id = legacy_app(tmp_path)
    original = db.get_turn(call_id, "legacy")
    assert original is not None
    double_encoded = json.dumps(original["final_text"], ensure_ascii=False)
    db.update_turn(turn_id, {"final_text": double_encoded})
    AudioStore(db).save(turn_id, "final", Audio(b"raw-json-audio", "audio/mpeg"))
    with client:
        shown = client.get(f"/calls/{call_id}/messages/legacy")
        audio = client.get(f"/calls/{call_id}/messages/legacy/audio/final")
    assert shown.json()["final_text"] == "이전 화면 답변"
    assert audio.status_code == 409


def test_triple_encoded_legacy_json_cannot_be_shown_or_replayed(tmp_path: Path) -> None:
    from app.audio_store import Audio, AudioStore
    client, db, call_id, turn_id = legacy_app(tmp_path)
    original = db.get_turn(call_id, "legacy")
    assert original is not None
    encoded = json.dumps(json.dumps(original["final_text"], ensure_ascii=False), ensure_ascii=False)
    db.update_turn(turn_id, {"final_text": encoded})
    AudioStore(db).save(turn_id, "final", Audio(b"raw-json-audio", "audio/mpeg"))
    with client:
        shown = client.get(f"/calls/{call_id}/messages/legacy")
        audio = client.get(f"/calls/{call_id}/messages/legacy/audio/final")
    assert shown.json()["final_text"] == "이전 화면 답변"
    assert audio.status_code == 409


def test_excessive_json_encoding_fails_closed(tmp_path: Path) -> None:
    from app.services import INSUFFICIENT_MESSAGE
    client, db, call_id, turn_id = legacy_app(tmp_path)
    original = db.get_turn(call_id, "legacy")
    assert original is not None
    encoded = str(original["final_text"])
    for _ in range(8):
        encoded = json.dumps(encoded, ensure_ascii=False)
    db.update_turn(turn_id, {"final_text": encoded})
    with client:
        shown = client.get(f"/calls/{call_id}/messages/legacy")
    assert shown.json()["final_text"] == INSUFFICIENT_MESSAGE


def test_legacy_session_history_hides_json_fields(tmp_path: Path) -> None:
    client, _, call_id, _ = legacy_app(tmp_path)
    with client:
        response = client.get(f"/sessions/{call_id}/history")
    assert response.json()[0]["final_text"] == "이전 화면 답변"


def test_new_hospital_outbox_payload_normalizes_legacy_json(tmp_path: Path) -> None:
    client, db, call_id, _ = legacy_app(tmp_path)
    with client:
        pass
    outbox = HospitalOutbox(db)
    assert outbox.enqueue_turn(call_id, "legacy")
    item = outbox._next()
    assert item is not None
    assert json.loads(item[2])["assistant_text"] == "이전 화면 답변"


def test_unmodified_legacy_pending_event_is_never_dispatched(tmp_path: Path) -> None:
    from contextlib import closing
    client, db, call_id, _ = legacy_app(tmp_path)
    with client:
        pass
    outbox = HospitalOutbox(db)
    assert outbox.enqueue_turn(call_id, "legacy")
    with closing(db.connect()) as con, con:
        row = con.execute("select event_id, body from hospital_outbox").fetchone()
        body = json.loads(row["body"])
        body["assistant_text"] = '{"display_text":"legacy", "internal":"secret"}'
        con.execute("update hospital_outbox set body = ? where event_id = ?", (json.dumps(body), row["event_id"]))
    result = anyio.run(outbox.dispatch, HospitalConfig("http://127.0.0.1:1", "fake"))
    assert result.pending == 1
    assert result.blocked_legacy == 1


def test_mismatched_pending_event_identity_is_blocked_before_http(tmp_path: Path) -> None:
    from contextlib import closing
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    client, db, call_id, _ = legacy_app(tmp_path)
    with client:
        pass
    outbox = HospitalOutbox(db)
    assert outbox.enqueue_turn(call_id, "legacy")
    with closing(db.connect()) as con, con:
        row = con.execute("select event_id, body from hospital_outbox").fetchone()
        body = json.loads(row["body"])
        body["call_id"] = "a-different-call"
        con.execute("update hospital_outbox set body = ? where event_id = ?", (json.dumps(body), row["event_id"]))
    received: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(True)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.5
        worker = Thread(target=server.handle_request)
        worker.start()
        result = anyio.run(outbox.dispatch, HospitalConfig(f"http://127.0.0.1:{server.server_port}", "fake"))
        worker.join(timeout=2)
    assert received == []
    assert result.pending == 1
    assert result.blocked_legacy == 1
