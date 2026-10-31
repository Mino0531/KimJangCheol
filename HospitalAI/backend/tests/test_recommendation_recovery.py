"""Recovery uses only confirmed recommendations with a current permission."""

from contextlib import closing
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.recommendation_navigation import confirm, snapshot, stage
from app.services import MockLLM, TTSResult


def confirmed_call(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / 'calls.sqlite')
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    generation = outbox.location_generation(call_id)
    assert generation is not None
    current = snapshot(database, call_id, generation)
    query = RecommendationQuery(latitude=35, longitude=126, specialty='내과')
    result = recommend(query, [HospitalCandidate(id='hospital', name='병원', latitude=35,
                                                  longitude=126, source='fixture')])
    token = stage(database, call_id, current, None, '내과', 'chosen', None, ('hospital',), result)
    assert token is not None
    assert confirm(database, call_id, token).outcome == 'confirmed'
    return database, call_id, token


def test_confirmed_recommendation_is_recovered_once(tmp_path: Path) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    assert recover_confirmed_recommendations(database) == 1
    assert recover_confirmed_recommendations(database) == 0
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') == 'pending'
    with closing(database.connect()) as con:
        body = con.execute('SELECT body FROM recommendation_outbox WHERE event_id=?', (f'rec:{token}',)).fetchone()
    assert json.loads(body['body'])['specialty'] == '내과'


def test_retry_worker_recovers_confirmed_recommendation(tmp_path: Path, monkeypatch) -> None:
    import anyio
    from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
    from app.municipal_retry import _retry

    database, call_id, token = confirmed_call(tmp_path)
    outbox = MunicipalOutbox(database)
    outbox.initialize()

    async def run() -> None:
        entered = anyio.Event()

        async def stop_after_dispatch(config: MunicipalConfig) -> None:
            entered.set()
            await anyio.sleep_forever()

        monkeypatch.setattr(outbox, 'dispatch', stop_after_dispatch)
        async with anyio.create_task_group() as group:
            group.start_soon(_retry, outbox, MunicipalConfig('http://127.0.0.1:1', 'fixture'))
            with anyio.fail_after(5):
                await entered.wait()
            group.cancel_scope.cancel()

    anyio.run(run)
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') == 'pending'


def test_unconfirmed_recommendation_is_never_recovered(tmp_path: Path) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("UPDATE call_recommendation_pending SET status='pending' WHERE token=?", (token,))
    assert recover_confirmed_recommendations(database) == 0
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') is None


def test_confirmed_recommendation_survives_normal_call_end(tmp_path: Path) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("UPDATE calls SET ended_at='2026-10-31T00:00:00Z', end_reason='hangup' WHERE id=?", (call_id,))
    assert recover_confirmed_recommendations(database) == 1
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') == 'pending'


@pytest.mark.parametrize('mutation', [
    "UPDATE calls SET state='declined' WHERE id=?",
    "UPDATE calls SET urgent=1 WHERE id=?",
    "UPDATE call_location_consent SET state='revoked' WHERE call_id=?",
    "UPDATE call_location_consent SET generation=generation+1 WHERE call_id=?",
])
def test_recovery_excludes_ineligible_permissions(tmp_path: Path, mutation: str) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute(mutation, (call_id,))
    assert recover_confirmed_recommendations(database) == 0
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') is None


def test_recovery_retries_after_pending_turn_finishes(tmp_path: Path) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    turn, _ = database.create_turn(call_id, 'pending', '궁금한 점이 있어요')
    assert recover_confirmed_recommendations(database) == 0
    database.update_turn(str(turn['id']), {'status': 'completed'})
    assert recover_confirmed_recommendations(database) == 1
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') == 'pending'


def test_withdrawal_after_recovery_selection_prevents_enqueue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    database, call_id, token = confirmed_call(tmp_path)
    original = RecommendationOutbox.enqueue

    def withdraw_before_enqueue(self, call_id, result, **kwargs):
        RecommendationOutbox(database).set_location_consent(call_id, False)
        return original(self, call_id, result, **kwargs)

    monkeypatch.setattr(RecommendationOutbox, 'enqueue', withdraw_before_enqueue)
    assert recover_confirmed_recommendations(database) == 0
    assert RecommendationOutbox(database).state(call_id, f'rec:{token}') is None


def test_http_confirm_and_hangup_recover_after_enqueue_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.recommendation_recovery import recover_confirmed_recommendations

    async def find(query, service_key):
        return recommend(query, [HospitalCandidate(id='hospital', name='병원', latitude=35,
                                                   longitude=126, source='fixture')])

    class AudioTTS:
        async def deliver(self, text, kind):
            return TTSResult(status='synthesized', kind=kind, text=text, audio=b'audio',
                             mime_type='audio/mpeg', interruptible=False)

    monkeypatch.setattr('app.call_api.public_recommendations', find)
    database = Database(tmp_path / 'http.sqlite')
    settings = Settings(environment='development', service_token='service', database_path=str(database.path))
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), AudioTTS())),
                    headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의'}).status_code == 200
        response = client.post(f'/calls/{call_id}/recommendations', json={
            'latitude': 35, 'longitude': 126, 'location_consent': True, 'include_audio': True})
        assert response.status_code == 200
        token = response.json()['navigation_token']
        with monkeypatch.context() as transient:
            transient.setattr(RecommendationOutbox, 'enqueue', lambda *args, **kwargs: None)
            confirmed = client.post(f'/calls/{call_id}/recommendations/confirm', json={'navigation_token': token})
        assert confirmed.status_code == 200 and confirmed.json()['confirmed']
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'}).status_code == 200
        assert recover_confirmed_recommendations(database) == 1
        assert RecommendationOutbox(database).state(call_id, f'rec:{token}') == 'pending'
