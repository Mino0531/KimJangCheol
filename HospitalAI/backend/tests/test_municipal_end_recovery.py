"""End-of-call delivery preserves consented caller words."""

import json
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def test_end_records_inflight_utterance_and_lifecycle_once(tmp_path: Path) -> None:
    db = Database(tmp_path / 'end-recovery.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        db.create_turn(call_id, 'pending', '무릎이 불편해요')

        assert client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'}).status_code == 200
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'caller'}).status_code == 200
        MunicipalOutbox(db).recover()

        with closing(db.connect()) as con:
            events = [json.loads(row[0]) for row in con.execute('SELECT body FROM municipal_outbox')]
        interrupted = [event for event in events if event['event_type'] == 'turn_interrupted']
        ended = [event for event in events if event['event_type'] == 'call_ended']
        assert len(interrupted) == 1 and interrupted[0]['user_text'] == '무릎이 불편해요'
        assert len(ended) == 1 and ended[0]['end_reason'] == 'hangup'


def test_recover_backfills_historical_cancelled_call_once(tmp_path: Path) -> None:
    db = Database(tmp_path / 'old-end.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        turn, _ = db.create_turn(call_id, 'past', '머리가 아파요')
        with closing(db.connect()) as con, con:
            con.execute("UPDATE calls SET ended_at='2026-10-30T00:00:00Z',end_reason='hangup' WHERE id=?", (call_id,))
            con.execute("UPDATE turns SET status='cancelled',error='call_ended' WHERE id=?", (turn['id'],))
        outbox = MunicipalOutbox(db)
        outbox.recover()
        assert outbox.recover() == 0
        with closing(db.connect()) as con:
            events = [json.loads(row[0]) for row in con.execute('SELECT body FROM municipal_outbox')]
        assert any(event['event_type'] == 'turn_interrupted' and event['user_text'] == '머리가 아파요' for event in events)
        assert any(event['event_type'] == 'call_ended' for event in events)


def test_old_service_error_rejection_is_requeued_only_once(tmp_path: Path) -> None:
    db = Database(tmp_path / 'old-error.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        turn, _ = db.create_turn(call_id, 'lost', '병원을 찾고 싶어요')
        db.update_turn(str(turn['id']), {'status': 'interrupted', 'error': 'service_error'})
        outbox = MunicipalOutbox(db)
        assert outbox.enqueue_interrupted(call_id, 'lost')
        event_id = f"interrupted:{turn['id']}"
        outbox._mark_failed(event_id, 422)
        outbox.recover()
        assert outbox.failed_count() == 0
        outbox._mark_failed(event_id, 422)
        outbox.recover()
        assert outbox.failed_count() == 1
