import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import anyio
import pytest
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.recommend_dashboard import RecommendationOutbox
from app.schemas import TurnInput
from app.services import MockLLM, MockTTS
from app.utterance_location import (
    StaleLocationGeneration,
    capture_location_generation,
    capture_location_revision,
    user_generation_visible,
    validate_source_generation,
)


def new_call(tmp_path: Path):
    database = Database(tmp_path / 'location-generation.sqlite')
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    return database, calls, call_id


def current_revision(database: Database, call_id: str) -> int:
    with closing(database.connect()) as con:
        return capture_location_revision(con, call_id)


def reaccept(consent: RecommendationOutbox, database: Database, call_id: str):
    return consent.set_location_consent(call_id, True, source_location_generation=0,
                                        source_location_revision=current_revision(database, call_id))


def test_generation_is_captured_in_transaction_and_reconsent_cannot_revive_old_text(tmp_path):
    database, _, call_id = new_call(tmp_path)
    with closing(database.connect()) as con:
        assert capture_location_generation(con, call_id) == 1
        assert user_generation_visible(con, call_id, 1)
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    with closing(database.connect()) as con:
        assert capture_location_generation(con, call_id) == 0
        assert not user_generation_visible(con, call_id, None)
        assert not user_generation_visible(con, call_id, 1)
    assert reaccept(consent, database, call_id)
    with closing(database.connect()) as con:
        assert capture_location_generation(con, call_id) == 2
        assert user_generation_visible(con, call_id, 2)
        assert not user_generation_visible(con, call_id, 1)
        assert not validate_source_generation(con, call_id, None)
        assert not validate_source_generation(con, call_id, 1)
        assert validate_source_generation(con, call_id, 2)


def test_urgent_same_text_has_separate_records_per_location_generation(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    assert calls.mark_urgent(call_id, '숨쉬기 힘들어요', source_location_generation=1)
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    assert reaccept(consent, database, call_id)
    assert calls.mark_urgent(call_id, '숨쉬기 힘들어요', source_location_generation=2)
    with closing(database.connect()) as con:
        rows = con.execute('SELECT user_location_generation,text FROM urgent_detail_events WHERE call_id=? ORDER BY rowid', (call_id,)).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(1, '숨쉬기 힘들어요'), (2, '숨쉬기 힘들어요')]


def test_old_patient_symptoms_are_not_combined_with_new_location_generation(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    first = calls.submit_turn(call_id, TurnInput(request_id='first', text='농약을 뿌렸어요'))
    assert first is not None and not first[2]
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    assert reaccept(consent, database, call_id)
    later = calls.submit_turn(call_id, TurnInput(request_id='later', text='어지러워요', source_location_generation=2))
    assert later is not None and not later[2]


def test_location_withdrawal_keeps_local_safety_detection_for_new_speech(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    assert RecommendationOutbox(database).set_location_consent(call_id, False)
    revision = current_revision(database, call_id)
    first = calls.submit_turn(call_id, TurnInput(request_id='first', text='농약을 뿌렸어요', source_location_generation=0,
                                                source_location_revision=revision))
    assert first is not None and not first[2]
    later = calls.submit_turn(call_id, TurnInput(request_id='later', text='어지러워요', source_location_generation=0,
                                                source_location_revision=revision))
    assert later is not None and later[2]


def test_stale_urgent_request_neither_latches_nor_stores_text(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    assert reaccept(consent, database, call_id)
    with pytest.raises(StaleLocationGeneration):
        calls.mark_urgent(call_id, '화순읍에서 숨이 막혀요', source_location_generation=1)
    with closing(database.connect()) as con:
        assert con.execute('SELECT urgent FROM calls WHERE id=?', (call_id,)).fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM urgent_detail_events WHERE call_id=?', (call_id,)).fetchone()[0] == 0


def test_stale_message_source_is_rejected_before_persistent_turn_creation(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    assert reaccept(consent, database, call_id)
    for source in (None, 1):
        with pytest.raises(StaleLocationGeneration):
            calls.submit_turn(call_id, TurnInput(request_id='stale', text='화순읍에 있어요', source_location_generation=source))
    with closing(database.connect()) as con:
        assert con.execute('SELECT count(*) FROM turns WHERE session_id=?', (call_id,)).fetchone()[0] == 0


def test_new_location_withdrawn_utterance_is_processed_locally(tmp_path):
    database = Database(tmp_path / 'local-followup.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        assert client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).status_code == 200
        request = {'request_id': 'local', 'text': '무릎이 불편해요', 'source_location_generation': 0,
                   'source_location_revision': current_revision(database, call_id)}
        decision = client.post(f'/calls/{call_id}/interpret', json=request)
        assert decision.status_code == 200 and decision.json()['intent'] == 'consultation'
        response = client.post(f'/calls/{call_id}/messages', json=request)
        assert response.status_code == 202
        with closing(database.connect()) as con:
            assert con.execute('SELECT user_location_generation FROM turns WHERE session_id=? AND request_id=?',
                               (call_id, 'local')).fetchone()[0] == 0


def test_http_delayed_old_generation_urgent_is_rejected_without_latching(tmp_path):
    database = Database(tmp_path / 'http-stale.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        consent = RecommendationOutbox(database)
        assert consent.set_location_consent(call_id, False)
        assert reaccept(consent, database, call_id)
        reply = client.post(f'/calls/{call_id}/urgent', json={'text': '화순읍에서 숨이 막혀요', 'source_location_generation': 1})
        assert reply.status_code == 409
        with closing(database.connect()) as con:
            assert con.execute('SELECT urgent FROM calls WHERE id=?', (call_id,)).fetchone()[0] == 0
            assert con.execute('SELECT count(*) FROM urgent_detail_events WHERE call_id=?', (call_id,)).fetchone()[0] == 0
            events = [json.loads(row[0]) for row in con.execute("SELECT body FROM municipal_outbox WHERE call_id=? AND event_type='urgent'", (call_id,))]
        assert events == []


def test_stale_zero_revision_urgent_cannot_latch_but_current_revision_can(tmp_path):
    database = Database(tmp_path / 'revision-stale.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        consent = RecommendationOutbox(database)
        assert consent.set_location_consent(call_id, False)
        with closing(database.connect()) as con:
            old_revision = capture_location_revision(con, call_id)
        assert consent.set_location_consent(call_id, True, source_location_generation=0,
                                            source_location_revision=old_revision)
        assert consent.set_location_consent(call_id, False)
        with closing(database.connect()) as con:
            current_revision = capture_location_revision(con, call_id)
        stale = {'text': '숨을 못 쉬어요', 'source_location_generation': 0, 'source_location_revision': old_revision}
        assert client.post(f'/calls/{call_id}/urgent', json=stale).status_code == 409
        with closing(database.connect()) as con:
            assert con.execute('SELECT urgent FROM calls WHERE id=?', (call_id,)).fetchone()[0] == 0
        fresh = {**stale, 'source_location_revision': current_revision}
        assert client.post(f'/calls/{call_id}/urgent', json=fresh).json()['urgent'] is True


def test_urgent_followup_rejects_delayed_utterance_from_previous_zero_epoch(tmp_path):
    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.calls = []

        async def _complete(self, model, system, user):
            self.calls.append(user)
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'followup-zero-epoch.sqlite')
    model = Low()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        consent = RecommendationOutbox(database)
        assert consent.set_location_consent(call_id, False)
        old_revision = current_revision(database, call_id)
        assert reaccept(consent, database, call_id)
        assert consent.set_location_consent(call_id, False)
        revision = current_revision(database, call_id)
        old = {'text': '화순읍에 있어요', 'source_location_generation': 0, 'source_location_revision': old_revision}
        assert client.post(f'/calls/{call_id}/urgent-followup', json=old).status_code == 409
        assert client.post(f'/calls/{call_id}/urgent-followup', json={**old, 'source_location_revision': revision}).status_code == 200
    assert model.calls == []
    with closing(database.connect()) as con:
        rows = con.execute('SELECT user_location_generation,user_location_revision FROM urgent_followups').fetchall()
    assert [tuple(row) for row in rows] == [(0, revision)]


def test_stale_urgent_followup_is_rejected_without_model_or_raw_storage(tmp_path):
    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.calls = []

        async def _complete(self, model, system, user):
            self.calls.append(user)
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'stale.sqlite')
    model = Low()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        consent = RecommendationOutbox(database)
        assert consent.set_location_consent(call_id, False)
        assert reaccept(consent, database, call_id)
        for request in ({'text': '화순읍에 있어요', 'source_location_generation': 1}, {'text': '화순읍에 있어요'}):
            assert client.post(f'/calls/{call_id}/urgent-followup', json=request).status_code == 409
    assert model.calls == []
    with closing(database.connect()) as con:
        assert con.execute('SELECT count(*) FROM urgent_followups WHERE call_id=?', (call_id,)).fetchone()[0] == 0


def test_same_text_in_new_generation_is_not_lost_due_to_legacy_urgent_event(tmp_path):
    database, calls, call_id = new_call(tmp_path)
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    assert calls.mark_urgent(call_id, '숨쉬기 힘들어요')
    legacy_id = f'urgent-detail:{call_id}'
    legacy = {'source': 'hospital-ai', 'event_id': legacy_id, 'call_id': call_id,
              'occurred_at': '2026-10-31T00:00:00Z', 'event_type': 'urgent', 'phone': '+821000000001',
              'user_text': '숨쉬기 힘들어요', 'assistant_text': '119에 연락하세요.',
              'answer_status': 'emergency', 'sources': []}
    with closing(database.connect()) as con, con:
        con.execute('DELETE FROM urgent_detail_events WHERE call_id=?', (call_id,))
        con.execute('UPDATE urgent_details SET user_location_generation=NULL,user_location_revision=NULL WHERE call_id=?', (call_id,))
        con.execute("INSERT INTO municipal_outbox(event_id,call_id,event_type,body,state) VALUES (?,?,'urgent',?,'sent')",
                    (legacy_id, call_id, json.dumps(legacy, ensure_ascii=False)))
    calls.initialize()
    consent = RecommendationOutbox(database)
    assert consent.set_location_consent(call_id, False)
    assert reaccept(consent, database, call_id)
    assert calls.mark_urgent(call_id, '숨쉬기 힘들어요', source_location_generation=2)
    outbox.recover()
    with closing(database.connect()) as con:
        events = [json.loads(row[0]) for row in con.execute("SELECT body FROM municipal_outbox WHERE event_id LIKE ?", (f'{legacy_id}:%',))]
    assert len(events) == 1 and events[0]['user_location_generation'] == 2


def test_urgent_followup_history_excludes_withdrawn_location_generation(tmp_path):
    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.payloads = []

        async def _complete(self, model, system, user):
            self.payloads.append(json.loads(user))
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'followup.sqlite')
    model = Low()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
        assert client.post(f'/calls/{call_id}/urgent').status_code == 200
        assert client.post(f'/calls/{call_id}/urgent-followup', json={'text': '화순읍에 혼자 있어요'}).status_code == 200
        consent = RecommendationOutbox(database)
        assert consent.set_location_consent(call_id, False)
        assert reaccept(consent, database, call_id)
        assert client.post(f'/calls/{call_id}/urgent-followup', json={'text': '옆집에 연락할게요', 'source_location_generation': 2}).status_code == 200
    assert '화순읍' not in json.dumps(model.payloads[-1], ensure_ascii=False)


def test_followup_generation_is_captured_before_blocking_model_request(tmp_path):
    class HeldLow(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.entered = Event()
            self.release = Event()

        async def _complete(self, model, system, user):
            self.entered.set()
            await anyio.to_thread.run_sync(self.release.wait)
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'race.sqlite')
    model = HeldLow()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        future = pool.submit(client.post, f'/calls/{call_id}/urgent-followup', json={'text': '화순에 있어요'})
        try:
            assert model.entered.wait(5)
            assert RecommendationOutbox(database).set_location_consent(call_id, False)
        finally:
            model.release.set()
        assert future.result(timeout=5).status_code == 409
    with closing(database.connect()) as con:
        assert con.execute('SELECT count(*) FROM urgent_followups WHERE call_id=?', (call_id,)).fetchone()[0] == 0


@pytest.mark.parametrize(('text', 'action'), [('끊을게요', 'end'), ('동의철회', 'revoke'), ('동의 철회해요', 'revoke'),
                                            ('아직 끊지 말고 기다려요', 'continue')])
def test_after_location_withdrawal_urgent_controls_are_handled_without_external_model(tmp_path, text, action):
    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.calls = []

        async def _complete(self, model, system, user):
            self.calls.append(user)
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'offline.sqlite')
    model = Low()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        assert RecommendationOutbox(database).set_location_consent(call_id, False)
        result = client.post(f'/calls/{call_id}/urgent-followup', json={'text': text, 'source_location_generation': 0,
                             'source_location_revision': current_revision(database, call_id)})
        assert result.status_code == 200 and result.json()['action'] == action
    assert model.calls == []


def test_withdrawal_before_dispatch_suppresses_external_urgent_input(tmp_path, monkeypatch):
    from app import call_urgent_followup

    entered, release = Event(), Event()
    original = call_urgent_followup.user_generation_visible

    def held_visibility(con, call_id, generation):
        entered.set()
        assert release.wait(5)
        return original(con, call_id, generation)

    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.calls = []

        async def _complete(self, model, system, user):
            self.calls.append(user)
            return '{"intent":"continue"}'

    database = Database(tmp_path / 'pre-dispatch-race.sqlite')
    model = Low()
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(model, MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        monkeypatch.setattr(call_urgent_followup, 'user_generation_visible', held_visibility)
        future = pool.submit(client.post, f'/calls/{call_id}/urgent-followup', json={'text': '화순읍에 혼자 있어요'})
        try:
            assert entered.wait(5)
            assert RecommendationOutbox(database).set_location_consent(call_id, False)
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 409
    assert model.calls == []


@pytest.mark.parametrize('spoken', ['위치 동의 철회', '위치는 더 쓰지 말아주세요'])
def test_urgent_location_only_revoke_keeps_emergency_call_active(tmp_path, spoken):
    class Low(MockLLM):
        class config:
            low_model = 'fake'
            allow_medical_data = True

        async def _complete(self, model, system, user):
            return '{"intent":"revoke_location"}'

    database = Database(tmp_path / 'location-revoke.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=database.path),
                     database, ServiceBundle(Low(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        client.post(f'/calls/{call_id}/urgent')
        reply = client.post(f'/calls/{call_id}/urgent-followup', json={'text': spoken})
        assert reply.status_code == 200 and reply.json()['action'] == 'revoke_location'
        with closing(database.connect()) as con:
            call = con.execute('SELECT state,urgent,ended_at FROM calls WHERE id=?', (call_id,)).fetchone()
            location = con.execute('SELECT state FROM call_location_consent WHERE call_id=?', (call_id,)).fetchone()
        assert tuple(call) == ('accepted', 1, None)
        assert location['state'] == 'revoked'
        assert reply.json()['location_generation'] == 0
        assert reply.json()['location_revision'] == current_revision(database, call_id)
