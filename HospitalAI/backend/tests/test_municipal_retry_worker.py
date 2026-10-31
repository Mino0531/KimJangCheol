"""Real TCP test for retrying a durable urgent notification after recovery."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Lock, Thread

import anyio
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.call_store import CallStore
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def test_lifespan_starts_location_delivery_worker(tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from app.municipal_retry import municipal_lifespan

    database = Database(tmp_path / 'worker.sqlite')
    database.initialize()
    CallStore(database).initialize()
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    started = Event()

    async def location_worker(db: Database, config: MunicipalConfig) -> None:
        assert db is database
        started.set()
        await anyio.sleep_forever()

    monkeypatch.setattr('app.municipal_recommend_retry.retry_loop', location_worker)
    app = FastAPI(lifespan=municipal_lifespan(outbox, MunicipalConfig('http://127.0.0.1:1', 'fixture')))
    with TestClient(app):
        assert started.wait(3)


def test_urgent_delivery_retries_after_receiver_recovers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.municipal_retry.RETRY_SECONDS", 0.05)
    healthy = Event()
    delivered = Event()
    attempts = []
    lock = Lock()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                attempts.append(body["event_type"])
            if not healthy.is_set():
                self.send_response(503)
                self.end_headers()
                return
            data = json.dumps({"applied": True, "duplicate": False, "event_id": body["event_id"],
                               "call_id": body["call_id"]}).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            if body["event_type"] == "urgent":
                delivered.set()

        def log_message(self, format: str, *args: str) -> None:
            return None

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    path = tmp_path / "retry.sqlite"
    settings = Settings(environment="development", database_path=str(path), service_token="service-token",
                        municipal_url=f"http://127.0.0.1:{receiver.server_port}", municipal_token="intake-token")
    try:
        app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
        with TestClient(app, headers={"Authorization": "Bearer service-token"}) as client:
            call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
            assert client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).status_code == 200
            assert client.post(f"/calls/{call_id}/urgent", json={}).status_code == 200
            with lock:
                assert attempts
            healthy.set()
            assert delivered.wait(5)
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()


def test_parallel_dispatch_is_idempotent_at_receiver(tmp_path: Path) -> None:
    barrier = Barrier(2, timeout=4)
    lock = Lock()
    received: set[str] = set()
    statuses: list[int] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            barrier.wait()
            with lock:
                duplicate = body["event_id"] in received
                received.add(body["event_id"])
                status = 200 if duplicate else 201
                statuses.append(status)
            data = json.dumps({"applied": not duplicate, "duplicate": duplicate,
                               "event_id": body["event_id"], "call_id": body["call_id"]}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: str) -> None:
            return None

    db = Database(tmp_path / "race.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    session = db.create_session("+821000000001", None)
    call_id = str(session["id"])
    CallStore(db).create(call_id)
    CallStore(db).decide(call_id, "accepted")
    CallStore(db).mark_urgent(call_id)
    assert outbox.enqueue_urgent(call_id)
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    worker = Thread(target=receiver.serve_forever, daemon=True)
    worker.start()
    config = MunicipalConfig(f"http://127.0.0.1:{receiver.server_port}", "intake-token")

    async def dispatch_together() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(outbox.dispatch, config)
            group.start_soon(outbox.dispatch, config)

    try:
        anyio.run(dispatch_together)
    finally:
        receiver.shutdown()
        receiver.server_close()
        worker.join()
    assert len(received) == 1
    assert sorted(statuses) == [200, 201]
    assert outbox._pending() == 0


def test_recovery_prioritizes_pending_urgent_over_start(tmp_path: Path) -> None:
    db = Database(tmp_path / "priority.sqlite")
    db.initialize()
    CallStore(db).initialize()
    outbox = MunicipalOutbox(db)
    outbox.initialize()
    session = db.create_session("+821000000001", None)
    call_id = str(session["id"])
    CallStore(db).create(call_id)
    CallStore(db).decide(call_id, "accepted")
    outbox.enqueue_start(call_id)
    CallStore(db).mark_urgent(call_id)
    outbox.enqueue_urgent(call_id)
    pending = outbox._next()
    assert pending is not None
    assert json.loads(pending[2])["event_type"] == "urgent"
