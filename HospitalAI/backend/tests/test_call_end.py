from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from app.audio_store import Audio, AudioStore
from app.call_capabilities import CallCapabilities
from app.call_end import end_call
from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle, process_turn
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import MockLLM, MockTTS, TTSResult, WAIT_MESSAGE


def test_end_preserves_consent_and_completed_records(tmp_path: Path) -> None:
    # Given a consented call with an already completed consultation.
    path = tmp_path / 'end.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        turn, _ = db.create_turn(call_id, 'completed', '무릎이 아파요')
        db.update_turn(str(turn['id']), {'status': 'completed', 'final_text': '약봉투를 챙겨 주세요.'})
        AudioStore(db).save(str(turn['id']), 'final', Audio(b'completed-audio', 'audio/mpeg'))
        MunicipalOutbox(db).enqueue_turn(call_id, 'completed')

        # When the caller ends the call, including a duplicate delivery.
        result = client.post(f'/calls/{call_id}/end', json={'reason': 'caller'})
        duplicate = client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'})

        # Then consent and completed records remain, while the first reason is stable.
        assert result.status_code == 200
        assert result.json()['state'] == 'ended'
        assert duplicate.json() == result.json()
        call = CallStore(db).get(call_id)
        assert call['state'] == 'accepted'
        assert call['end_reason'] == 'caller'
        assert db.get_turn(call_id, 'completed')['final_text'] == '약봉투를 챙겨 주세요.'
        assert client.get(f'/calls/{call_id}/messages/completed').status_code == 200
        assert client.get(f'/calls/{call_id}/messages/completed/audio/final').content == b'completed-audio'
        assert client.get(f'/calls/{call_id}').json()['lifecycle'] == 'ended'
        assert MunicipalOutbox(db)._pending() == 3


def test_end_blocks_new_processing_and_late_writes(tmp_path: Path) -> None:
    # Given a consented call with an in-flight turn.
    path = tmp_path / 'pending.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        turn, _ = db.create_turn(call_id, 'pending', '무릎이 아파요')

        # When the call ends before the turn completes.
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'connection_lost'}).status_code == 200

        # Then new processing and delayed answers are refused.
        assert client.post(f'/calls/{call_id}/messages', json={'request_id': 'new', 'text': '추가 질문'}).status_code == 409
        assert client.get(f'/calls/{call_id}/prompts/question/audio').status_code == 409
        assert not CallStore(db).update_turn_if_safe(str(turn['id']), {'status': 'completed', 'final_text': '늦은 답변'})
        assert not CallStore(db).save_audio_if_safe(str(turn['id']), 'final', Audio(b'late', 'audio/mpeg'))
        assert db.get_turn(call_id, 'pending')['status'] == 'cancelled'
        assert db.get_turn(call_id, 'pending')['error'] == 'call_ended'


def test_end_is_call_scoped_and_does_not_revoke_urgent_flag(tmp_path: Path) -> None:
    # Given two calls and a capability scoped to only one of them.
    path = tmp_path / 'scope.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        a = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        b = client.post('/calls', json={'phone': '+821000000002'}).json()['id']
        token = CallCapabilities(db, 's', 'development').issue(a)
        CallStore(db).mark_urgent(a)

        # When the scoped caller tries to end another call, then its own urgent call.
        denied = client.post(f'/calls/{b}/end', json={'reason': 'caller'}, headers={'Authorization': f'Bearer {token}'})
        ended = client.post(f'/calls/{a}/end', json={'reason': 'caller'}, headers={'Authorization': f'Bearer {token}'})

        # Then scope and emergency history remain intact.
        assert denied.status_code == 401
        assert ended.status_code == 200
        assert CallStore(db).get(b)['ended_at'] is None
        assert CallStore(db).is_urgent(a)


def test_end_reason_validation_preserves_distinct_consent_state(tmp_path: Path) -> None:
    path = tmp_path / 'reason.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'unknown'}).status_code == 422
        assert CallStore(db).get(call_id)['ended_at'] is None
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'safety_stop'},
                           headers={'Authorization': ''}).status_code == 401
        result = client.post(f'/calls/{call_id}/end', json={'reason': 'safety_stop'})
        assert result.json()['reason'] == 'safety_stop'
        assert CallStore(db).get(call_id)['state'] == 'awaiting_consent'
        assert not CallStore(db).decide(call_id, 'accepted')
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의'}).status_code == 409


def test_end_preserves_queued_recommendation_and_allows_explicit_withdrawal(tmp_path: Path) -> None:
    path = tmp_path / 'recommendation.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        result = recommend(RecommendationQuery(latitude=35, longitude=126, specialty='내과'), [
            HospitalCandidate(id='a', name='가상 병원', latitude=35.01, longitude=126,
                              specialties=('내과',), source='test')])
        outbox = RecommendationOutbox(db)
        queued = outbox.enqueue(call_id, result, specialty='내과', location_consent=True)
        assert queued is not None

        assert client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'}).status_code == 200

        assert outbox.state(call_id, queued[0]) == 'pending'
        assert outbox.enqueue(call_id, result, specialty='내과', location_consent=True) is None
        assert outbox.set_location_consent(call_id, True) is None
        assert client.post(f'/calls/{call_id}/location-consent', json={'accepted': True}).status_code == 409
        assert client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).status_code == 200
        assert outbox.state(call_id, queued[0]) == 'cancelled'
        assert client.post(f'/calls/{call_id}/revoke').status_code == 200
        assert CallStore(db).get(call_id)['state'] == 'declined'
        assert CallStore(db).get(call_id)['end_reason'] == 'hangup'


@pytest.mark.parametrize('stage', ['reasoning', 'tts'])
def test_end_suppresses_inflight_provider_completion(tmp_path: Path, stage: str) -> None:
    path = tmp_path / 'inflight.sqlite'
    db = Database(path)
    create_app(Settings(environment='development', service_token='s', database_path=str(path), seed_demo=True),
               db, ServiceBundle(MockLLM(), MockTTS()))
    session = db.create_session('+821000000001', None)
    call_id = str(session['id'])
    CallStore(db).create(call_id)
    assert CallStore(db).decide(call_id, 'accepted')
    turn, _ = db.create_turn(call_id, 'inflight', '기침이 나요')

    async def scenario() -> None:
        started, release, done = anyio.Event(), anyio.Event(), anyio.Event()

        class DelayedModel:
            async def low(self, text: str) -> str:
                return WAIT_MESSAGE

            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                if stage == 'reasoning':
                    started.set()
                    with anyio.CancelScope(shield=True):
                        await release.wait()
                return '신분증을 챙겨 주세요.'

        class DelayedSpeech:
            async def deliver(self, text: str, kind: str) -> TTSResult:
                if kind == 'final' and stage == 'tts':
                    started.set()
                    with anyio.CancelScope(shield=True):
                        await release.wait()
                return TTSResult('synthesized', kind, text, False, b'late-audio', 'audio/mpeg')

        async def run_turn() -> None:
            await process_turn(db, ServiceBundle(DelayedModel(), DelayedSpeech()), session, turn)
            done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(run_turn)
            with anyio.fail_after(5):
                await started.wait()
                assert await anyio.to_thread.run_sync(end_call, db, call_id, 'hangup') is not None
                release.set()
                await done.wait()

    anyio.run(scenario)
    saved = db.get_turn(call_id, 'inflight')
    assert saved['status'] == 'cancelled'
    assert saved['error'] == 'call_ended'
    assert saved['final_text'] is None
    assert AudioStore(db).get(str(turn['id']), 'final') is None
    assert not MunicipalOutbox(db).enqueue_turn(call_id, 'inflight')


def test_old_call_table_receives_lifecycle_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / 'migration.sqlite')
    db.initialize()
    session = db.create_session('+821000000001', None)
    with db.connect() as con:
        con.execute("""CREATE TABLE calls (id TEXT PRIMARY KEY, state TEXT NOT NULL,
            consent_version TEXT NOT NULL, decision_at TEXT, created_at TEXT NOT NULL)""")
        con.execute("INSERT INTO calls VALUES (?, 'accepted', 'municipality-v2', NULL, '2026-01-01')",
                    (session['id'],))
        con.commit()

    CallStore(db).initialize()

    call = CallStore(db).get(str(session['id']))
    assert call['state'] == 'accepted'
    assert call['ended_at'] is None
    assert call['end_reason'] is None


@pytest.mark.parametrize('endpoint', ['interrupt', 'playback-complete'])
def test_end_between_auth_and_write_preserves_completed_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              endpoint: str) -> None:
    path = tmp_path / 'auth-race.sqlite'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='s', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        turn, _ = db.create_turn(call_id, 'completed', '병원 준비물을 알려주세요')
        db.update_turn(str(turn['id']), {'status': 'completed', 'final_text': '신분증을 챙겨 주세요.',
                                        'tts_json': '{"final":"synthesized"}'})
        AudioStore(db).save(str(turn['id']), 'final', Audio(b'completed-audio', 'audio/mpeg'))
        original_get = CallStore.get
        armed = True

        def end_after_auth_read(store: CallStore, session_id: str):
            nonlocal armed
            row = original_get(store, session_id)
            if armed and session_id == call_id:
                armed = False
                assert end_call(db, call_id, 'hangup') is not None
            return row

        # /end commits after auth read but before the handler's write transaction.
        monkeypatch.setattr(CallStore, 'get', end_after_auth_read)
        response = client.post(f'/calls/{call_id}/messages/completed/{endpoint}', json={})

        assert response.status_code == 409
        assert db.get_turn(call_id, 'completed')['status'] == 'completed'
        assert db.get_turn(call_id, 'completed')['final_text'] == '신분증을 챙겨 주세요.'
        assert AudioStore(db).get(str(turn['id']), 'final').data == b'completed-audio'
        with db.connect() as con:
            assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='playback_completed'").fetchone()[0] == 0
