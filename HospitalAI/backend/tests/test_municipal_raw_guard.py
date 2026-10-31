"""Withdrawn caller speech cannot be sent from the municipal outbox."""

import json
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import anyio
import pytest

from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalOutbox
from app.recommend_dashboard import RecommendationOutbox


@pytest.fixture
def call(tmp_path: Path):
    db = Database(tmp_path / 'guard.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    call_id = str(db.create_session('+821000000001')['id'])
    calls.create(call_id)
    calls.decide(call_id, 'accepted')
    box = MunicipalOutbox(db)
    box.initialize()
    return db, call_id, box, RecommendationOutbox(db)


def insert(db, call_id, event_id, generation, *, urgent=False):
    payload = {'source': 'hospital-ai', 'event_id': event_id, 'call_id': call_id,
               'occurred_at': '2026-10-31T00:00:00Z', 'event_type': 'urgent' if urgent else 'caller_utterance',
               'phone': '+821000000001', 'user_text': '화순 동면에서 아파요',
               'assistant_text': '119에 연락해 주세요.' if urgent else '',
               'answer_status': 'emergency' if urgent else None, 'sources': [],
               'user_location_generation': generation}
    if not urgent:
        payload['utterance_id'] = 'utterance-1'
    with closing(db.connect()) as con, con:
        MunicipalOutbox._insert(con, payload)


def redacted_payload(db, original_id):
    with closing(db.connect()) as con:
        row = con.execute('''SELECT o.body FROM municipal_redacted_events r
            JOIN municipal_outbox o ON o.event_id=r.replacement_event_id
            WHERE r.original_event_id=?''', (original_id,)).fetchone()
    return json.loads(row['body']) if row else None


def test_withdrawn_pending_utterance_is_replaced_without_changing_original(call):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:original', 1)
    recommendation.set_location_consent(call_id, False)

    event = box._next()

    assert event is None  # Capability-specific worker handles the replacement.
    delivered = redacted_payload(db, 'utterance:original')
    assert delivered is not None
    assert '화순' not in json.dumps(delivered, ensure_ascii=False)
    assert delivered['event_type'] == 'privacy_redacted'
    assert delivered['assistant_text'] == '' and delivered['answer_status'] is None
    with closing(db.connect()) as con:
        original = con.execute('SELECT body FROM municipal_outbox WHERE event_id=?', ('utterance:original',)).fetchone()
    assert json.loads(original['body'])['user_text'] == '화순 동면에서 아파요'


def test_urgent_event_survives_with_raw_text_removed(call):
    db, call_id, box, recommendation = call
    CallStore(db).mark_urgent(call_id)
    insert(db, call_id, f'urgent:{call_id}', 1, urgent=True)
    recommendation.set_location_consent(call_id, False)

    event = box._next()

    assert event is not None
    body = json.loads(event[2])
    assert body['event_type'] == 'urgent' and body['user_text'] == ''
    assert '화순' not in event[2]


def test_prewithdrawal_selection_rechecked_before_send(call):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:original', 1)
    chosen = box._next()
    assert chosen is not None and '화순' in chosen[2]
    recommendation.set_location_consent(call_id, False)

    assert not box._eligible(chosen[0], call_id)
    event = box._next()
    assert event is None
    assert '화순' not in json.dumps(redacted_payload(db, 'utterance:original'), ensure_ascii=False)


def test_already_interrupted_raw_event_still_produces_safe_staff_notice(call):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:original', 1)
    with closing(db.connect()) as con, con:
        con.execute('INSERT INTO municipal_interruptions(event_id) VALUES (?)', ('utterance:original',))
    recommendation.set_location_consent(call_id, False)

    event = box._next()

    assert event is None
    replacement = redacted_payload(db, 'utterance:original')
    assert replacement is not None and replacement['event_type'] == 'privacy_redacted'
    assert '화순' not in json.dumps(replacement, ensure_ascii=False)


def test_legacy_unsent_redaction_is_migrated_without_rewriting_event(call):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:original', 1)
    recommendation.set_location_consent_with_epoch(call_id, False)
    old = {'source': 'hospital-ai', 'event_id': 'redacted:old', 'call_id': call_id,
           'occurred_at': '2026-10-31T00:00:00Z', 'event_type': 'turn_interrupted',
           'phone': '+821000000001', 'user_text': '위치 정보가 포함될 수 있어 원문 확인 제한',
           'assistant_text': '응답이 중단됐습니다.', 'answer_status': 'interrupted', 'sources': []}
    with closing(db.connect()) as con, con:
        con.execute('''CREATE TABLE municipal_redacted_events (
            original_event_id TEXT PRIMARY KEY, replacement_event_id TEXT NOT NULL)''')
        MunicipalOutbox._insert(con, old)
        con.execute('INSERT INTO municipal_redacted_events VALUES (?,?)', ('utterance:original', 'redacted:old'))
        con.execute('INSERT INTO municipal_interruptions VALUES (?)', ('utterance:original',))

    assert box._next() is None
    replacement = redacted_payload(db, 'utterance:original')
    assert replacement is not None and replacement['event_type'] == 'privacy_redacted'
    with closing(db.connect()) as con:
        original = json.loads(con.execute('SELECT body FROM municipal_outbox WHERE event_id=?', ('redacted:old',)).fetchone()[0])
    assert original == old


@pytest.mark.parametrize('generation', [None, 0, 1])
def test_old_or_unknown_generation_stays_hidden_after_reconsent(call, generation):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:old', generation)
    withdrawal = recommendation.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None
    recommendation.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3])

    event = box._next()

    assert event is None
    assert '화순' not in json.dumps(redacted_payload(db, 'utterance:old'), ensure_ascii=False)


def test_new_generation_after_reconsent_remains_visible(call):
    db, call_id, box, recommendation = call
    withdrawal = recommendation.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None
    recommendation.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3])
    insert(db, call_id, 'utterance:new', 2)

    event = box._next()

    assert event is not None and '화순' in event[2]


def test_impossible_future_generation_is_never_sent(call):
    db, call_id, box, _ = call
    insert(db, call_id, 'utterance:future', 99)

    assert box._next() is None
    replacement = redacted_payload(db, 'utterance:future')
    assert replacement is not None and replacement['event_type'] == 'privacy_redacted'


def test_unverified_location_revision_is_never_sent(call):
    db, call_id, box, _ = call
    insert(db, call_id, 'utterance:revision', 1)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE municipal_outbox SET body=json_set(body,'$.user_location_revision',999) WHERE event_id=?",
                    ('utterance:revision',))

    assert box._next() is None
    assert redacted_payload(db, 'utterance:revision')['event_type'] == 'privacy_redacted'


def test_delayed_old_generation_arriving_after_reconsent_is_never_sent(call):
    db, call_id, box, recommendation = call
    withdrawal = recommendation.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None
    recommendation.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3])
    # The delayed event retains the generation of the original speech, not
    # the generation current when it reaches the server.
    insert(db, call_id, 'utterance:delayed-old', 1)

    event = box._next()

    assert event is None
    replacement = redacted_payload(db, 'utterance:delayed-old')
    assert replacement is not None and '화순' not in json.dumps(replacement, ensure_ascii=False)
    with closing(db.connect()) as con:
        stored = con.execute('SELECT body FROM municipal_outbox WHERE event_id=?', ('utterance:delayed-old',)).fetchone()
    assert json.loads(stored['body'])['user_location_generation'] == 1


def test_no_personal_notification_after_full_consent_withdrawal(call):
    db, call_id, box, recommendation = call
    insert(db, call_id, 'utterance:old', 1)
    recommendation.set_location_consent(call_id, False)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))

    assert box._next() is None


def test_actual_http_retries_safe_urgent_before_other_events(call):
    from app.municipal_bridge import MunicipalConfig

    db, call_id, box, recommendation = call
    CallStore(db).mark_urgent(call_id)
    insert(db, call_id, f'urgent:{call_id}', 1, urgent=True)
    box.enqueue_start(call_id)
    recommendation.set_location_consent(call_id, False)
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(payload)
            ack = json.dumps({'applied': True, 'duplicate': False,
                              'event_id': payload['event_id'], 'call_id': payload['call_id']}).encode()
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(ack)))
            self.end_headers()
            self.wfile.write(ack)

        def log_message(self, format, *args):
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        config = MunicipalConfig(f'http://127.0.0.1:{server.server_port}', 'fixture')
        anyio.run(box.dispatch, config)
        anyio.run(box.dispatch, config)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()

    assert [item['event_type'] for item in received] == ['urgent', 'call_started']
    assert received[0]['user_text'] == ''
    assert '화순' not in json.dumps(received, ensure_ascii=False)
