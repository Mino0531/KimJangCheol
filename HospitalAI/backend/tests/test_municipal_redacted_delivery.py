"""Privacy notices use a compatible receiver and never block ordinary intake."""

from contextlib import closing, contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Lock, Thread

import anyio
import pytest

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.municipal_redacted_delivery import dispatch_redacted


def queued(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / 'notices.sqlite')
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    MunicipalOutbox(database).initialize()
    event_id = 'redacted:' + sha256(b'fixture').hexdigest()
    payload = {'source': 'hospital-ai', 'event_id': event_id, 'call_id': call_id,
               'occurred_at': '2026-10-31T00:00:00Z', 'event_type': 'privacy_redacted',
               'phone': '+821000000001', 'user_text': '위치 정보가 포함될 수 있어 원문 확인 제한',
               'assistant_text': '', 'answer_status': None, 'sources': []}
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE municipal_redacted_events (
            original_event_id TEXT PRIMARY KEY REFERENCES municipal_outbox(event_id), replacement_event_id TEXT NOT NULL)''')
        con.execute("INSERT INTO municipal_outbox(event_id,call_id,event_type,body) VALUES (?,?,?,?)",
                    ('original', call_id, 'caller_utterance', '{}'))
        con.execute("INSERT INTO municipal_interruptions(event_id) VALUES ('original')")
        con.execute('INSERT INTO municipal_outbox(event_id,call_id,event_type,body) VALUES (?,?,?,?)',
                    (event_id, call_id, 'privacy_redacted', json.dumps(payload, ensure_ascii=False)))
        con.execute('INSERT INTO municipal_redacted_events VALUES (?,?)', ('original', event_id))
    return database, call_id, event_id


@contextmanager
def server(capability_status=200, capability=b'{"privacy_redacted":true}', *, on_get=None, on_post=None, wrong_ack=False):
    received = []
    seen = set()
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(('GET', self.path, self.headers.get('Authorization')))
            if on_get:
                on_get()
            self.send_response(capability_status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(capability)))
            self.end_headers()
            self.wfile.write(capability)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(('POST', self.path, body))
            with lock:
                duplicate = body['event_id'] in seen
                seen.add(body['event_id'])
            if on_post:
                on_post()
            ack = json.dumps({'applied': not duplicate, 'duplicate': duplicate,
                              'event_id': 'wrong' if wrong_ack else body['event_id'], 'call_id': body['call_id']}).encode()
            self.send_response(200 if duplicate else 201)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(ack)))
            self.end_headers()
            self.wfile.write(ack)

        def log_message(self, format, *args):
            return None

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield MunicipalConfig(f'http://127.0.0.1:{http.server_port}', 'fixture-token'), received
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_capable_receiver_gets_exact_notice_once(tmp_path: Path):
    db, call_id, event_id = queued(tmp_path)
    with server() as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
        again = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == 1 and again.delivered == 0
    assert [item[0] for item in received] == ['GET', 'POST']
    assert received[0][2] == 'Bearer fixture-token'
    assert received[1][2]['event_id'] == event_id and received[1][2]['call_id'] == call_id
    with closing(db.connect()) as con:
        assert con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'sent'


@pytest.mark.parametrize(('status', 'body'), [(404, b'{}'), (401, b'{}'), (500, b'{}'),
    (200, b'invalid'), (200, b'{}'), (200, b'{"privacy_redacted":false}'),
    (200, b'{"privacy_redacted":1}')])
def test_unsupported_receiver_keeps_notice_pending(tmp_path: Path, status: int, body: bytes):
    db, _, event_id = queued(tmp_path)
    with server(status, body) as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == 0 and result.pending == 1
    assert all(item[0] != 'POST' for item in received)
    with closing(db.connect()) as con:
        assert con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'pending'


def test_consent_withdrawal_during_capability_request_prevents_post(tmp_path: Path):
    db, call_id, _ = queued(tmp_path)
    def revoke():
        with closing(db.connect()) as con, con:
            con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
    with server(on_get=revoke) as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == 0
    assert all(item[0] != 'POST' for item in received)


def test_invalid_ack_leaves_same_body_for_retry(tmp_path: Path):
    db, _, event_id = queued(tmp_path)
    with server(wrong_ack=True) as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == 0 and result.pending == 1
    assert [item[0] for item in received] == ['GET', 'POST']
    with closing(db.connect()) as con:
        row = con.execute('SELECT body,state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()
    assert row['state'] == 'pending' and json.loads(row['body']) == received[1][2]


def test_active_interpretation_and_unsent_urgent_defer_notice(tmp_path: Path):
    db, call_id, event_id = queued(tmp_path)
    with closing(db.connect()) as con, con:
        con.execute("INSERT INTO municipal_interpretation_gates VALUES ('gate',?,unixepoch('now')+60)", (call_id,))
    with server() as (config, received):
        assert anyio.run(dispatch_redacted, db, config).pending == 1
        with closing(db.connect()) as con, con:
            con.execute("DELETE FROM municipal_interpretation_gates WHERE id='gate'")
            con.execute('UPDATE calls SET urgent=1 WHERE id=?', (call_id,))
        assert anyio.run(dispatch_redacted, db, config).pending == 1
        with closing(db.connect()) as con, con:
            con.execute("INSERT INTO municipal_outbox(event_id,call_id,event_type,body,state) VALUES (?,?,'urgent','{}','sent')",
                        ('urgent', call_id))
        assert anyio.run(dispatch_redacted, db, config).delivered == 1
    assert [item[0] for item in received].count('POST') == 1
    with closing(db.connect()) as con:
        assert con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'sent'


def test_full_consent_withdrawal_during_post_prevents_retry(tmp_path: Path):
    db, call_id, event_id = queued(tmp_path)
    def revoke():
        with closing(db.connect()) as con, con:
            con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
    with server(on_post=revoke) as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
        again = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == again.delivered == 0
    assert [item[0] for item in received] == ['GET', 'POST']
    with closing(db.connect()) as con:
        assert con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'pending'


def test_invalid_notice_never_sends_unchecked_text(tmp_path: Path):
    db, _, event_id = queued(tmp_path)
    with closing(db.connect()) as con, con:
        raw = con.execute('SELECT body FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0]
        body = json.loads(raw)
        body['user_text'] = 'private raw text'
        con.execute('UPDATE municipal_outbox SET body=? WHERE event_id=?', (json.dumps(body), event_id))
    with server() as (config, received):
        result = anyio.run(dispatch_redacted, db, config)
    assert result.delivered == 0 and result.pending == 0
    assert all(item[0] != 'POST' for item in received)
    with closing(db.connect()) as con:
        assert con.execute('SELECT status_code FROM municipal_failures WHERE event_id=?', (event_id,)).fetchone()[0] == 422


def test_concurrent_workers_apply_one_notice(tmp_path: Path):
    db, _, event_id = queued(tmp_path)
    results = []
    async def run(config):
        async def worker():
            results.append(await dispatch_redacted(db, config))
        async with anyio.create_task_group() as group:
            group.start_soon(worker)
            group.start_soon(worker)
    with server() as (config, received):
        anyio.run(run, config)
    assert sum(result.delivered for result in results) == 1
    posts = [item[2] for item in received if item[0] == 'POST']
    assert posts and {item['event_id'] for item in posts} == {event_id}
    with closing(db.connect()) as con:
        assert con.execute('SELECT state FROM municipal_outbox WHERE event_id=?', (event_id,)).fetchone()[0] == 'sent'


def test_real_municipal_receiver_accepts_notice_over_http(tmp_path: Path, monkeypatch):
    pytest.importorskip('sqlmodel')  # Dashboard's optional dependency is not part of the phone package.
    from fastapi.testclient import TestClient

    monkeypatch.setenv('HOSPITAL_DB_PATH', str(tmp_path / 'receiver-default.sqlite'))
    monkeypatch.setenv('MUNICIPAL_INTAKE_TOKEN', 'fixture-intake')
    monkeypatch.setenv('MUNICIPAL_STAFF_TOKEN', 'fixture-staff')
    monkeypatch.setenv('HOSPITAL_DASHBOARD_TOKEN', 'fixture-hospital')
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'external' / 'KimJangCheol'))
    from backend.app import app as default_dashboard, create_app

    database, call_id, event_id = queued(tmp_path)
    dashboard = create_app(tmp_path / 'receiver.sqlite')
    received = []
    with TestClient(dashboard) as client:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply(client.get(self.path, headers={'Authorization': self.headers.get('Authorization', '')}))

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append(json.loads(body))
                self.reply(client.post(self.path, headers={'Authorization': self.headers.get('Authorization', ''),
                                                           'Content-Type': 'application/json'}, content=body))

            def reply(self, response):
                self.send_response(response.status_code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, format, *args):
                return None

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            config = MunicipalConfig(f'http://127.0.0.1:{http.server_port}', 'fixture-intake')
            assert anyio.run(dispatch_redacted, database, config).delivered == 1
            result = client.get(f'/api/municipality/inbox/{call_id}',
                                headers={'Authorization': 'Bearer fixture-staff'})
            assert result.status_code == 200
            assert result.json()['events'][0]['event_type'] == 'privacy_redacted'
            assert received[0]['event_id'] == event_id
            assert '화순' not in json.dumps(received, ensure_ascii=False)
        finally:
            http.shutdown()
            http.server_close()
            thread.join()
    dashboard.state.engine.dispose()
    default_dashboard.state.engine.dispose()
