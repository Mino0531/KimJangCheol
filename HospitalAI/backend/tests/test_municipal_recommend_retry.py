"""Recommendation worker requires authenticated receiver privacy support."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import json
from pathlib import Path
from threading import Thread

import anyio
import pytest

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalConfig
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend


def setup_queue(tmp_path: Path) -> tuple[Database, str, RecommendationOutbox]:
    db = Database(tmp_path / 'retry.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    call_id = str(db.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    outbox = RecommendationOutbox(db)
    candidate = HospitalCandidate(id='h', name='가상 병원', latitude=35, longitude=126, source='fixture')
    result = recommend(RecommendationQuery(latitude=35, longitude=126), [candidate])
    assert outbox.enqueue(call_id, result, specialty=None, location_consent=True)
    return db, call_id, outbox


@pytest.mark.parametrize('reply', [None, False, 1, 'true', 'malformed', 'unauthorized', 'missing', 'null_value'])
def test_capability_failure_never_sends_sensitive_events(tmp_path: Path, reply) -> None:
    from app.municipal_recommend_retry import retry_once

    database, _, outbox = setup_queue(tmp_path)
    received = []

    class Server(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(('GET', self.headers.get('Authorization')))
            status = 401 if reply == 'unauthorized' else 404 if reply is None else 200
            capability = {} if reply == 'missing' else {'location_tombstones': None if reply == 'null_value' else reply}
            body = b'not json' if reply == 'malformed' else json.dumps(capability).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            received.append(('POST', None))
            self.send_response(500)
            self.end_headers()

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'intake-fixture')
        outcome = anyio.run(retry_once, database, config)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert outcome.delivered == 0 and outbox._pending() == 1
    assert received == [('GET', 'Bearer intake-fixture')]


def test_no_pending_event_skips_capability_request(tmp_path: Path) -> None:
    from app.municipal_recommend_retry import retry_once

    database, _, _ = setup_queue(tmp_path)
    with closing(database.connect()) as connection, connection:
        connection.execute("UPDATE recommendation_outbox SET state='sent'")
    requests = []

    class Server(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = anyio.run(retry_once, database,
                           MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'fixture'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.delivered == 0 and result.pending == 0
    assert requests == []


def test_upgrade_delivery_and_withdrawal_are_replay_safe(tmp_path: Path) -> None:
    from app.municipal_recommend_retry import retry_once

    database, call_id, outbox = setup_queue(tmp_path)
    received = []
    support = {'enabled': False}

    class Server(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({'location_tombstones': support['enabled']}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            event = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(event)
            body = json.dumps({'applied': True, 'duplicate': False, 'event_id': event['event_id'],
                               'call_id': event['call_id']}).encode()
            self.send_response(201)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'fixture')
        assert anyio.run(retry_once, database, config).delivered == 0
        support['enabled'] = True
        assert anyio.run(retry_once, database, config).delivered == 1
        assert anyio.run(retry_once, database, config).delivered == 0
        assert outbox.set_location_consent(call_id, False)
        assert anyio.run(retry_once, database, config).delivered == 1
        assert anyio.run(retry_once, database, config).delivered == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert [item['event_type'] for item in received] == ['recommendation', 'location_revoked']
    assert received[0]['location_generation'] == received[1]['location_generation']


def test_lost_ack_retries_same_event_without_duplicate_receiver_record(tmp_path: Path) -> None:
    from app.municipal_recommend_retry import retry_once

    database, _, outbox = setup_queue(tmp_path)
    seen = set()
    attempts = []

    class Server(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"location_tombstones":true}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            event = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            duplicate = event['event_id'] in seen
            seen.add(event['event_id'])
            attempts.append(event)
            body = json.dumps({'applied': not duplicate, 'duplicate': duplicate, 'event_id': event['event_id'],
                               'call_id': event['call_id']}).encode()
            self.send_response(200 if duplicate else 503)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'fixture')
        assert anyio.run(retry_once, database, config).delivered == 0
        assert outbox._pending() == 1
        assert anyio.run(retry_once, database, config).delivered == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert len(seen) == 1 and len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert outbox._pending() == 0
