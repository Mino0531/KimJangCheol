"""Playback acknowledgement does not assert that the caller heard the audio."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

from app.audio_store import Audio, AudioStore
from app.call_capabilities import CallCapabilities
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def create_completed(client: TestClient, db: Database) -> tuple[str, str]:
    call = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
    assert client.post(f'/calls/{call}/consent', json={'text': '동의'}).status_code == 200
    turn, _ = db.create_turn(call, 'request', '기침이 납니다')
    db.update_turn(str(turn['id']), {'status': 'completed', 'final_text': '상담 내용',
                   'tts_json': json.dumps({'final': 'synthesized'})})
    AudioStore(db).save(str(turn['id']), 'final', Audio(b'fake audio', 'audio/mpeg'))
    return call, str(turn['id'])


def test_playback_event_is_atomic_and_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / 'playback.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=db.path)
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization': 'Bearer s'}) as client:
        call, turn_id = create_completed(client, db)
        url = f'/calls/{call}/messages/request/playback-complete'
        first = client.post(url, json={})
        second = client.post(url, json={})
        assert first.status_code == second.status_code == 200
        assert first.json()['status'] == 'playback_completed'
        with closing(db.connect()) as con:
            records = con.execute('SELECT body FROM municipal_outbox WHERE event_id=?',
                                  (f'playback:{turn_id}',)).fetchall()
        assert len(records) == 1
        event = json.loads(records[0]['body'])
        assert event['event_type'] == 'playback_completed'
        assert event['user_text'] == event['assistant_text'] == ''
        assert event['answer_status'] is None and event['sources'] == []


def test_interrupted_or_missing_audio_cannot_acknowledge_playback(tmp_path: Path) -> None:
    db = Database(tmp_path / 'states.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=db.path)
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization': 'Bearer s'}) as client:
        call, turn_id = create_completed(client, db)
        AudioStore(db).save(turn_id, 'final', Audio(b'', 'audio/mpeg'))
        url = f'/calls/{call}/messages/request/playback-complete'
        assert client.post(url, json={}).status_code == 409
        assert client.post(f'/calls/{call}/messages/request/interrupt', json={}).status_code == 200
        assert client.post(url, json={}).status_code == 409


def test_concurrent_interrupt_and_playback_never_make_interrupted_turn_completed(tmp_path: Path) -> None:
    db = Database(tmp_path / 'race.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=db.path)
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization': 'Bearer s'}) as client:
        call, _ = create_completed(client, db)
        barrier = Barrier(2)

        def post(suffix: str) -> int:
            barrier.wait()
            return client.post(f'/calls/{call}/messages/request/{suffix}', json={}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            ack = pool.submit(post, 'playback-complete')
            interrupt = pool.submit(post, 'interrupt')
            assert ack.result() in (200, 409)
            assert interrupt.result() == 200
        assert db.get_turn(call, 'request')['status'] == 'interrupted'


def test_other_call_token_cannot_acknowledge_playback(tmp_path: Path) -> None:
    db = Database(tmp_path / 'auth.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=db.path)
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization': 'Bearer s'}) as client:
        call, turn_id = create_completed(client, db)
        other = client.post('/calls', json={'phone': '+821000000002'}).json()['id']
        token = CallCapabilities(db, 's', 'development').issue(other)
        result = client.post(f'/calls/{call}/messages/request/playback-complete', json={},
                             headers={'Authorization': f'Bearer {token}'})
        assert result.status_code == 401
        with closing(db.connect()) as con:
            assert con.execute('SELECT 1 FROM municipal_outbox WHERE event_id=?',
                               (f'playback:{turn_id}',)).fetchone() is None


def test_revoked_consent_cannot_acknowledge_playback(tmp_path: Path) -> None:
    db = Database(tmp_path / 'revoked.sqlite')
    settings = Settings(environment='development', service_token='s', database_path=db.path)
    with TestClient(create_app(settings, db, ServiceBundle(MockLLM(), MockTTS())),
                    headers={'Authorization': 'Bearer s'}) as client:
        call, turn_id = create_completed(client, db)
        assert client.post(f'/calls/{call}/revoke', json={}).status_code == 200
        assert client.post(f'/calls/{call}/messages/request/playback-complete', json={}).status_code == 403
        with closing(db.connect()) as con:
            assert con.execute('SELECT 1 FROM municipal_outbox WHERE event_id=?',
                               (f'playback:{turn_id}',)).fetchone() is None
