"""Real SQLite outbox and local HTTP receiver tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from pathlib import Path
from threading import Thread

import anyio
import json
import pytest

from app.call_store import CallStore
from app.db import Database
from app.hospital_bridge import HospitalConfig, HospitalOutbox, InvalidHospitalConfig


def seed(tmp_path: Path) -> tuple[Database, str]:
    db = Database(tmp_path / "hospital.sqlite3")
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    session = db.create_session("+821000000001")
    session_id = str(session["id"])
    calls.create(session_id)
    turn, _ = db.create_turn(session_id, "case-1", "가상 기침 사례")
    db.update_turn(str(turn["id"]), {
        "status": "completed", "final_text": "생성된 가상 답변",
        "sources_json": json.dumps([{"id": "clinic:1", "type": "clinic", "source": "sample", "text": "private"}]),
    })
    return db, session_id


def test_only_explicitly_consented_final_turn_enters_outbox(tmp_path: Path) -> None:
    db, session_id = seed(tmp_path)
    outbox = HospitalOutbox(db)
    outbox.initialize()
    assert outbox.enqueue_turn(session_id, "case-1") is False
    CallStore(db).decide(session_id, "accepted")
    assert outbox.enqueue_turn(session_id, "case-1") is True
    assert outbox.enqueue_turn(session_id, "case-1") is False


def test_failure_keeps_event_and_retry_reuses_identical_payload(tmp_path: Path) -> None:
    db, session_id = seed(tmp_path)
    CallStore(db).decide(session_id, "accepted")
    outbox = HospitalOutbox(db)
    outbox.initialize()
    assert outbox.enqueue_turn(session_id, "case-1")
    received: list[bytes] = []
    paths: list[str] = []
    credentials: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.rfile.read(int(self.headers["content-length"])))
            paths.append(self.path)
            credentials.append(self.headers.get("Authorization"))
            event = json.loads(received[-1])
            body = json.dumps({"applied": True, "duplicate": False, "event_id": event["event_id"], "call_id": event["call_id"]}).encode()
            self.send_response(503 if len(received) == 1 else 201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: str) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = HospitalConfig(f"http://127.0.0.1:{server.server_port}", "local-token")
        first = anyio.run(outbox.dispatch, config)
        second = anyio.run(outbox.dispatch, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert first.delivered == 0 and first.pending == 1
    assert second.delivered == 1 and second.pending == 0
    assert received[0] == received[1]
    assert paths == ["/api/team/consultation-results", "/api/team/consultation-results"]
    assert credentials == ["Bearer local-token", "Bearer local-token"]
    body = json.loads(received[1])
    assert body["assistant_text"] == "생성된 가상 답변"
    assert body["sources"] == [{"id": "clinic:1", "type": "clinic", "source": "sample"}]


def test_revoked_consent_is_never_sent(tmp_path: Path) -> None:
    db, session_id = seed(tmp_path)
    CallStore(db).decide(session_id, "accepted")
    outbox = HospitalOutbox(db)
    outbox.initialize()
    assert outbox.enqueue_turn(session_id, "case-1")
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET state = 'declined' WHERE id = ?", (session_id,))
    result = anyio.run(outbox.dispatch, HospitalConfig("http://127.0.0.1:1", "local-token"))
    assert result.delivered == 0 and result.pending == 1


def test_invalid_receiver_ack_does_not_mark_delivered(tmp_path: Path) -> None:
    db, session_id = seed(tmp_path)
    CallStore(db).decide(session_id, "accepted")
    outbox = HospitalOutbox(db)
    outbox.initialize()
    assert outbox.enqueue_turn(session_id, "case-1")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"not an acknowledgement")

        def log_message(self, format: str, *args: str) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = anyio.run(outbox.dispatch, HospitalConfig(f"http://127.0.0.1:{server.server_port}", "local-token"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.delivered == 0 and result.pending == 1


def test_rejects_unsafe_remote_transport_and_missing_token() -> None:
    with pytest.raises(InvalidHospitalConfig):
        HospitalConfig("http://example.com", "token")
    with pytest.raises(InvalidHospitalConfig):
        HospitalConfig("https://example.com", "")


def test_recover_after_process_interruption_queues_only_consented_turns(tmp_path: Path) -> None:
    db, session_id = seed(tmp_path)
    outbox = HospitalOutbox(db)
    outbox.initialize()
    assert outbox.recover() == 0
    CallStore(db).decide(session_id, "accepted")
    assert outbox.recover() == 1
    assert outbox.recover() == 0
