"""Consent, replay and urgent routing for municipality delivery."""

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import anyio

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.schemas import TurnInput
from pydantic import ValidationError
import pytest


def _call(db: Database, phone: str = "+821000000001") -> str:
    session = db.create_session(phone, None)
    call_id = str(session["id"])
    CallStore(db).create(call_id)
    return call_id


def test_start_and_urgent_require_new_consent_and_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "events.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    call_id = _call(db)
    assert not outbox.enqueue_start(call_id)
    assert not outbox.enqueue_urgent(call_id)
    CallStore(db).decide(call_id, "accepted")
    assert outbox.enqueue_start(call_id)
    assert not outbox.enqueue_start(call_id)
    CallStore(db).mark_urgent(call_id)
    assert outbox.enqueue_urgent(call_id)
    assert not outbox.enqueue_urgent(call_id)
    with closing(db.connect()) as con:
        events = [json.loads(row[0]) for row in con.execute("SELECT body FROM municipal_outbox ORDER BY rowid")]
    assert [event["event_type"] for event in events] == ["call_started", "urgent"]
    assert events[1]["answer_status"] == "emergency"


def test_legacy_consent_never_transmits_to_municipality(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    call_id = _call(db)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET state='accepted', consent_version='prototype-v1', urgent=1 WHERE id=?", (call_id,))
    assert outbox.recover() == 0
    assert not outbox.enqueue_start(call_id)
    assert not outbox.enqueue_urgent(call_id)


def test_transport_requires_ack_and_replays_once(tmp_path: Path) -> None:
    requests: list[dict] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            status = 201 if len(requests) == 1 else 200
            reply = json.dumps({"applied": status == 201, "duplicate": status == 200,
                                "event_id": body["event_id"], "call_id": body["call_id"]}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: str) -> None:
            return None

    db = Database(tmp_path / "delivery.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    call_id = _call(db)
    CallStore(db).decide(call_id, "accepted")
    outbox.enqueue_start(call_id)
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    try:
        config = MunicipalConfig(f"http://127.0.0.1:{receiver.server_port}", "test-token")
        assert anyio.run(outbox.dispatch, config).delivered == 1
        assert anyio.run(outbox.dispatch, config).delivered == 0
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()
    assert len(requests) == 1


def test_whitespace_only_utterance_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TurnInput(request_id="blank", text="   ")


def test_invalid_event_does_not_block_other_calls(tmp_path: Path) -> None:
    requests: list[dict] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            status = 422 if body["phone"] == "+821000000001" else 201
            reply = json.dumps({"applied": True, "duplicate": False,
                                "event_id": body["event_id"], "call_id": body["call_id"]}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: str) -> None:
            return None

    db = Database(tmp_path / "rejected.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    for phone in ("+821000000001", "+821000000002"):
        call_id = _call(db, phone)
        CallStore(db).decide(call_id, "accepted")
        outbox.enqueue_start(call_id)
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    try:
        result = anyio.run(outbox.dispatch, MunicipalConfig(f"http://127.0.0.1:{receiver.server_port}", "test"))
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()
    assert result.delivered == 1
    assert result.pending == 0
    assert outbox.failed_count() == 1
    assert len(requests) == 2


def test_urgent_latch_waits_for_alert_before_call_started(tmp_path: Path) -> None:
    db = Database(tmp_path / "urgent-priority.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    call_id = _call(db)
    CallStore(db).decide(call_id, "accepted")
    outbox.enqueue_start(call_id)
    CallStore(db).mark_urgent(call_id)
    assert outbox._next() is None
    outbox.enqueue_urgent(call_id)
    item = outbox._next()
    assert item is not None and json.loads(item[2])["event_type"] == "urgent"


def test_corrupt_local_event_is_quarantined_before_other_calls(tmp_path: Path) -> None:
    requests: list[dict] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            reply = json.dumps({"applied": True, "duplicate": False,
                                "event_id": body["event_id"], "call_id": body["call_id"]}).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: str) -> None:
            return None

    db = Database(tmp_path / "corrupt.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    for phone in ("+821000000001", "+821000000002"):
        call_id = _call(db, phone)
        CallStore(db).decide(call_id, "accepted")
        outbox.enqueue_start(call_id)
        if phone.endswith("001"):
            with closing(db.connect()) as con, con:
                con.execute("UPDATE municipal_outbox SET body='invalid json' WHERE call_id=?", (call_id,))
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    try:
        result = anyio.run(outbox.dispatch, MunicipalConfig(f"http://127.0.0.1:{receiver.server_port}", "test"))
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()
    assert result.delivered == 1
    assert outbox.failed_count() == 1
    assert len(requests) == 1
