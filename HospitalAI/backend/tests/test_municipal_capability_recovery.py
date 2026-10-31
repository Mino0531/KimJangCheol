"""Caller-utterance recovery preserves consent and receiver compatibility."""

from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Thread

import anyio
import pytest

from app import municipal_capability_recovery
from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox


def failed_event(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / 'calls.sqlite')
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    event_id = 'utterance:test'
    body = json.dumps({'source': 'hospital-ai', 'event_id': event_id, 'call_id': call_id,
                       'occurred_at': '2026-10-31T00:00:00Z', 'event_type': 'caller_utterance',
                       'phone': '+821000000001', 'user_text': 'a synthetic utterance', 'utterance_id': 'test',
                       'assistant_text': '', 'answer_status': None, 'sources': []}, separators=(',', ':'))
    with closing(database.connect()) as con, con:
        con.execute('INSERT INTO municipal_outbox(event_id,call_id,event_type,body) VALUES (?,?,?,?)',
                    (event_id, call_id, 'caller_utterance', body))
        con.execute('INSERT INTO municipal_failures(event_id,status_code) VALUES (?,422)', (event_id,))
    return database, call_id, body


@contextmanager
def receiver(status: int, content: bytes, *, on_get=None, on_post=None):
    requests: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get('Authorization')))
            if self.headers.get('Authorization') != 'Bearer fixture-intake':
                self.send_response(401)
                self.end_headers()
                return
            if on_get is not None:
                on_get()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            if self.headers.get('Authorization') != 'Bearer fixture-intake':
                self.send_response(401)
                self.end_headers()
                return
            raw = self.rfile.read(int(self.headers['Content-Length']))
            event = json.loads(raw)
            requests.append((self.path, raw.decode('utf-8')))
            content = json.dumps({'applied': True, 'duplicate': False,
                'event_id': event['event_id'], 'call_id': event['call_id']}).encode()
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            if on_post is not None:
                on_post(raw)

        def log_message(self, format: str, *args: str) -> None:
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'fixture-intake'), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def failures(database: Database) -> list[str]:
    with closing(database.connect()) as con:
        return [str(row['event_id']) for row in con.execute('SELECT event_id FROM municipal_failures')]


def test_capable_receiver_releases_failed_event_once_unchanged(tmp_path: Path) -> None:
    database, call_id, original_body = failed_event(tmp_path)
    with receiver(200, b'{"caller_utterance":true}') as (config, requests):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 1
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 0
    assert requests == [('/api/municipality/intake/capabilities', 'Bearer fixture-intake')]
    assert failures(database) == []
    with closing(database.connect()) as con:
        row = con.execute('SELECT call_id,event_type,body,state FROM municipal_outbox').fetchone()
        assert (row['call_id'], row['event_type'], row['body'], row['state']) == (
            call_id, 'caller_utterance', original_body, 'pending')


@pytest.mark.parametrize(('status', 'content'), [
    (401, b'{}'), (404, b'{}'), (500, b'{}'), (200, b'not-json'),
    (200, b'{"caller_utterance":false}'), (200, b'{"caller_utterance":1}'),
    (200, b'{}'), (200, b'null'), (200, b'\xff'),
])
def test_unavailable_or_unsupported_receiver_never_releases_event(tmp_path: Path, status: int, content: bytes) -> None:
    database, _, _ = failed_event(tmp_path)
    with receiver(status, content) as (config, _):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 0
    assert failures(database) == ['utterance:test']


def test_withdrawn_consent_and_other_failures_are_not_retried(tmp_path: Path) -> None:
    database, call_id, _ = failed_event(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
    with receiver(200, b'{"caller_utterance":true}') as (config, requests):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 0
    assert requests == []
    assert failures(database) == ['utterance:test']


def test_second_422_is_not_released_again(tmp_path: Path) -> None:
    database, _, _ = failed_event(tmp_path)
    with receiver(200, b'{"caller_utterance":true}') as (config, _):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 1
        with closing(database.connect()) as con, con:
            con.execute("INSERT INTO municipal_failures(event_id,status_code) VALUES ('utterance:test',422)")
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 0
    assert failures(database) == ['utterance:test']


def test_capability_with_wrong_intake_token_does_not_release_event(tmp_path: Path) -> None:
    database, _, _ = failed_event(tmp_path)
    with receiver(200, b'{"caller_utterance":true}') as (config, _):
        wrong = MunicipalConfig(config.base_url, 'wrong-token')
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, wrong) == 0
    assert failures(database) == ['utterance:test']


def test_consent_withdrawn_during_capability_request_prevents_release(tmp_path: Path) -> None:
    database, call_id, _ = failed_event(tmp_path)

    def revoke() -> None:
        with closing(database.connect()) as con, con:
            con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))

    with receiver(200, b'{"caller_utterance":true}', on_get=revoke) as (config, _):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 0
    assert failures(database) == ['utterance:test']


def test_only_422_caller_utterances_are_released(tmp_path: Path) -> None:
    database, call_id, original_body = failed_event(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute('INSERT INTO municipal_outbox(event_id,call_id,event_type,body) VALUES (?,?,?,?)',
                    ('other', call_id, 'turn_completed', original_body))
        con.execute("INSERT INTO municipal_failures(event_id,status_code) VALUES ('other',422)")
    with receiver(200, b'{"caller_utterance":true}') as (config, _):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 1
    assert failures(database) == ['other']


def test_released_utterance_is_sent_unchanged_over_http(tmp_path: Path) -> None:
    database, _, original_body = failed_event(tmp_path)
    with receiver(200, b'{"caller_utterance":true}') as (config, requests):
        assert anyio.run(municipal_capability_recovery.recover_failed_utterances, database, config) == 1
        result = anyio.run(MunicipalOutbox(database).dispatch, config)
    assert result.delivered == 1
    assert requests[1] == ('/api/municipality/intake', original_body)


def test_running_retry_worker_recovers_and_sends_caller_utterance(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.orchestrator import ServiceBundle
    from app.services import MockLLM, MockTTS

    database, _, original_body = failed_event(tmp_path)
    delivered = Event()
    monkeypatch.setattr('app.municipal_retry.RETRY_SECONDS', 0.05)

    def note_delivery(raw: bytes) -> None:
        if raw.decode('utf-8') == original_body:
            delivered.set()

    with receiver(200, b'{"caller_utterance":true}', on_post=note_delivery) as (config, _):
        app = create_app(Settings(environment='development', database_path=database.path,
            municipal_url=config.base_url, municipal_token=config.token),
            database, ServiceBundle(MockLLM(), MockTTS()))
        with TestClient(app):
            assert delivered.wait(4)
    assert failures(database) == []


def test_urgent_dispatch_does_not_wait_for_capability_check(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.orchestrator import ServiceBundle
    from app.services import MockLLM, MockTTS

    database, call_id, _ = failed_event(tmp_path)
    assert CallStore(database).mark_urgent(call_id)
    assert MunicipalOutbox(database).enqueue_urgent(call_id)
    urgent_delivered = Event()
    monkeypatch.setattr('app.municipal_retry.RETRY_SECONDS', 0.05)

    def wait_for_urgent() -> None:
        urgent_delivered.wait(3)

    def note_delivery(raw: bytes) -> None:
        if json.loads(raw)['event_type'] == 'urgent':
            urgent_delivered.set()

    with receiver(200, b'{"caller_utterance":true}', on_get=wait_for_urgent, on_post=note_delivery) as (config, _):
        app = create_app(Settings(environment='development', database_path=database.path,
            municipal_url=config.base_url, municipal_token=config.token),
            database, ServiceBundle(MockLLM(), MockTTS()))
        with TestClient(app):
            assert urgent_delivered.wait(2)
