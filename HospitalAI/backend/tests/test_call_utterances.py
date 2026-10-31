import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import anyio
import pytest
from fastapi.testclient import TestClient

from app.audio_store import AudioStore
from app.call_end import end_call
from app.call_facts import initialize as initialize_facts
from app.call_revocation import revoke_call
from app.call_store import CallStore
from app.call_utterances import PendingUtterances
from app.config import Settings
from app.db import Database, DuplicateRequest
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.patient_subject_guard import session_subject_is_safe
from app.recommend_dashboard import RecommendationOutbox
from app.services import MockLLM, MockTTS, SplitLLM


@pytest.fixture
def setup(tmp_path: Path):
    db = Database(tmp_path / 'utterance.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    AudioStore(db).initialize()
    MunicipalOutbox(db).initialize()
    utterances = PendingUtterances(db)
    utterances.initialize()
    call_id = str(db.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    return db, calls, utterances, call_id


def test_consultation_classification_after_hangup_preserves_unanswered_speech(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '무릎이 아파요', 'general')
    end_call(db, call_id, 'hangup')
    utterances.classified(call_id, 'one', 'consultation')
    with closing(db.connect()) as con:
        row = con.execute("SELECT input_text,status,error FROM turns WHERE session_id=? AND request_id='one'", (call_id,)).fetchone()
        event = con.execute("SELECT event_type,body FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()
    assert tuple(row) == ('무릎이 아파요', 'cancelled', 'call_ended')
    assert event and '무릎이 아파요' in event['body']


def test_duplicate_id_rejects_changed_text(setup):
    _, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '무릎이 아파요', 'general')
    with pytest.raises(DuplicateRequest):
        utterances.admit(call_id, 'one', '허리가 아파요', 'general')


def test_withdrawal_classification_never_retains_or_sends_raw_speech(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '개인정보 동의를 철회해요', 'general')
    end_call(db, call_id, 'hangup')
    utterances.classified(call_id, 'one', 'revoke_consent')
    with closing(db.connect()) as con:
        assert con.execute('SELECT count(*) FROM pending_utterances').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 0


def test_existing_turn_is_not_duplicated_when_call_ends(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '무릎이 아파요', 'general')
    utterances.classified(call_id, 'one', 'consultation')
    db.create_turn(call_id, 'one', '무릎이 아파요')
    end_call(db, call_id, 'hangup')
    with closing(db.connect()) as con:
        assert con.execute("SELECT count(*) FROM turns WHERE session_id=? AND request_id='one'", (call_id,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 1


def test_classified_utterance_survives_end_before_message_admission(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '무릎이 아파요', 'general')
    utterances.classified(call_id, 'one', 'consultation')
    end_call(db, call_id, 'hangup')
    with closing(db.connect()) as con:
        row = con.execute("SELECT input_text FROM turns WHERE session_id=? AND request_id='one'", (call_id,)).fetchone()
        assert row and row['input_text'] == '무릎이 아파요'
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 1


def test_consent_revocation_erases_pending_and_prevents_delayed_delivery(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '무릎이 아파요', 'general')
    assert revoke_call(db, call_id)
    utterances.classified(call_id, 'one', 'consultation')
    with closing(db.connect()) as con:
        assert con.execute('SELECT count(*) FROM pending_utterances').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 0


def test_recommend_request_is_stored_once_as_caller_only_event(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '다른 병원 찾아 주세요', 'general')
    utterances.classified(call_id, 'one', 'recommend')
    utterances.classified(call_id, 'one', 'recommend')
    with closing(db.connect()) as con:
        rows = con.execute("SELECT body FROM municipal_outbox WHERE event_type='caller_utterance'").fetchall()
    assert len(rows) == 1
    event = json.loads(rows[0]['body'])
    assert event['utterance_id'] == 'one' and event['user_text'] == '다른 병원 찾아 주세요'
    assert event['assistant_text'] == '' and event['answer_status'] is None


def test_control_only_speech_does_not_claim_answer_was_interrupted(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'one', '잠깐 기다려요', 'general')
    end_call(db, call_id, 'hangup')
    utterances.classified(call_id, 'one', 'consultation', False)
    with closing(db.connect()) as con:
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 0


def test_http_hangup_during_intent_classification_recovers_utterance(tmp_path: Path):
    class HeldLow:
        class config:
            low_model = 'fake'
            allow_medical_data = True

        def __init__(self):
            self.entered = Event()
            self.resume = Event()

        async def _complete(self, model, system, user):
            self.entered.set()
            await anyio.to_thread.run_sync(self.resume.wait)
            return '{"intent":"consultation"}'

    db = Database(tmp_path / 'http.sqlite')
    provider = HeldLow()
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client, ThreadPoolExecutor() as threads:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
        pending = threads.submit(client.post, f'/calls/{call_id}/interpret',
                                 json={'request_id': 'voice-1', 'text': '무릎이 아파요'})
        try:
            assert provider.entered.wait(5)
            assert client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'}).status_code == 200
        finally:
            provider.resume.set()
        assert pending.result(timeout=5).status_code == 409
        with closing(db.connect()) as con:
            assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 1
            assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 1


def test_http_control_only_does_not_become_false_interruption(tmp_path: Path):
    class ControlLow:
        class config:
            low_model = 'fake'
            allow_medical_data = True

        async def _complete(self, model, system, user):
            return '{"intent":"consultation","voice_controls":{"hold":true,"control_only":true}}'

    db = Database(tmp_path / 'control.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(SplitLLM(ControlLow(), MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
        assert client.post(f'/calls/{call_id}/interpret', json={'request_id': 'voice-1', 'text': '잠깐만요'}).status_code == 200
        assert client.post(f'/calls/{call_id}/end', json={'reason': 'hangup'}).status_code == 200
        with closing(db.connect()) as con:
            assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 1
            assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='turn_interrupted'").fetchone()[0] == 0


def test_utterance_location_generation_is_captured_at_admission_not_classification(setup):
    db, _, utterances, call_id = setup
    outbox = RecommendationOutbox(db)
    assert utterances.admit(call_id, 'first', '화순읍에서 무릎이 아파요', 'general')
    utterances.classified(call_id, 'first', 'consultation')
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    assert utterances.admit(call_id, 'second', '손목도 아파요', 'general', 0, revision)
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=revision)
    assert utterances.admit(call_id, 'third', '기침도 나요', 'general', 2)
    with closing(db.connect()) as con:
        rows = con.execute('SELECT request_id,user_location_generation FROM pending_utterances ORDER BY seq').fetchall()
        event = con.execute("SELECT body FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()
    assert [(row['request_id'], row['user_location_generation']) for row in rows] == [
        ('first', 1), ('second', 0), ('third', 2),
    ]
    assert json.loads(event['body'])['user_location_generation'] == 1
    end_call(db, call_id, 'hangup')
    with closing(db.connect()) as con:
        recovered = con.execute("SELECT user_location_generation FROM turns WHERE session_id=? AND request_id='first'", (call_id,)).fetchone()
    assert recovered is None


def test_legacy_utterance_migration_never_invents_a_location_generation(tmp_path: Path):
    db = Database(tmp_path / 'legacy.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    call_id = str(db.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    with closing(db.connect()) as con, con:
        con.execute("""CREATE TABLE pending_utterances (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL,
            request_id TEXT NOT NULL, input_text TEXT NOT NULL, caller_context TEXT NOT NULL,
            state TEXT NOT NULL, intent TEXT, created_at TEXT NOT NULL,
            UNIQUE(call_id,request_id))""")
        con.execute("""INSERT INTO pending_utterances(call_id,request_id,input_text,caller_context,state,intent,created_at)
            VALUES (?,'old','화순읍','general','accepted','consultation','2026-01-01T00:00:00.000Z')""", (call_id,))
    PendingUtterances(db).initialize()
    with closing(db.connect()) as con:
        assert con.execute('SELECT user_location_generation FROM pending_utterances').fetchone()[0] is None


def test_delayed_old_generation_utterance_is_not_admitted(setup):
    db, _, utterances, call_id = setup
    outbox = RecommendationOutbox(db)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=revision)
    assert not utterances.admit(call_id, 'stale', '화순읍입니다', 'general', 1)
    assert not utterances.admit(call_id, 'legacy', '화순읍입니다', 'general')
    assert utterances.admit(call_id, 'new', '기침이 나요', 'general', 2)
    with closing(db.connect()) as con:
        assert con.execute('SELECT count(*) FROM pending_utterances').fetchone()[0] == 1
        assert con.execute("SELECT user_location_generation FROM pending_utterances WHERE request_id='new'").fetchone()[0] == 2


def test_http_call_responses_expose_current_location_generation(tmp_path: Path):
    db = Database(tmp_path / 'response.sqlite')
    app = create_app(Settings(environment='development', service_token='s', database_path=db.path),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer s'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        decision = client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).json()
        assert type(decision['location_generation']) is int
        assert decision['location_generation'] == 1
        assert type(decision['location_revision']) is int and decision['location_revision'] > 0
        assert client.get(f'/calls/{call_id}').json()['location_generation'] == 1
        assert client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).status_code == 200
        after = client.get(f'/calls/{call_id}').json()
        assert after['location_generation'] == 0
        assert type(after['location_revision']) is int and after['location_revision'] > decision['location_revision']


def test_delayed_old_zero_generation_is_rejected_after_second_withdrawal(setup):
    db, _, utterances, call_id = setup
    outbox = RecommendationOutbox(db)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        old_revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=old_revision)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        current_revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    assert not utterances.admit(call_id, 'old', '화순읍', 'general', 0, old_revision)
    assert not utterances.admit(call_id, 'unknown', '화순읍', 'general', 0)
    assert utterances.admit(call_id, 'current', '기침', 'general', 0, current_revision)


def test_old_utterance_id_cannot_be_reused_with_new_generation(setup):
    db, _, utterances, call_id = setup
    outbox = RecommendationOutbox(db)
    assert utterances.admit(call_id, 'one', '화순읍', 'general', 1)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=revision)
    assert not utterances.admit(call_id, 'one', '화순읍', 'general', 2)


def test_late_zero_generation_classification_does_not_emit_after_new_zero_period(setup):
    db, _, utterances, call_id = setup
    outbox = RecommendationOutbox(db)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        old_revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    assert utterances.admit(call_id, 'old', '화순읍', 'general', 0, old_revision)
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=old_revision)
    outbox.set_location_consent(call_id, False)
    assert not utterances.classified(call_id, 'old', 'consultation')
    with closing(db.connect()) as con:
        assert con.execute('SELECT count(*) FROM pending_utterances').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 0


def test_proxy_classification_atomically_marks_subject_boundary(setup):
    db, _, utterances, call_id = setup
    initialize_facts(db)
    assert utterances.admit(call_id, 'proxy', '어머니가 아프세요', 'general')
    assert utterances.classified(call_id, 'proxy', 'consultation', True, True, False)
    with closing(db.connect()) as con:
        assert con.execute("SELECT state FROM pending_utterances WHERE request_id='proxy'").fetchone()[0] == 'accepted'
        assert not session_subject_is_safe(con, call_id)
        assert con.execute('SELECT count(*) FROM call_fact_subject_boundaries WHERE call_id=?', (call_id,)).fetchone()[0] == 1


def test_proxy_followup_does_not_create_another_boundary_but_new_patient_does(setup):
    db, _, utterances, call_id = setup
    initialize_facts(db)
    for request_id, text, changed in [('proxy', '어머니가 아프세요', False),
                                      ('followup', '어머니는 무릎이 아프세요', False),
                                      ('new', '이번에는 아버지예요', True)]:
        assert utterances.admit(call_id, request_id, text, 'general')
        assert utterances.classified(call_id, request_id, 'consultation', True, True, changed)
    with closing(db.connect()) as con:
        ids = [row[0] for row in con.execute('SELECT utterance_id FROM call_fact_subject_boundaries WHERE call_id=? ORDER BY source_sequence', (call_id,))]
    assert ids == ['proxy', 'new']


def test_proxy_classification_fails_closed_when_boundary_schema_is_missing(setup):
    db, _, utterances, call_id = setup
    assert utterances.admit(call_id, 'proxy', '어머니가 아프세요', 'general')
    assert not utterances.classified(call_id, 'proxy', 'consultation', True, True, False)
    with closing(db.connect()) as con:
        assert con.execute("SELECT state FROM pending_utterances WHERE request_id='proxy'").fetchone()[0] == 'pending'
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 0


def test_patient_change_is_guarded_even_when_caller_ends_in_same_utterance(setup):
    db, _, utterances, call_id = setup
    initialize_facts(db)
    assert utterances.admit(call_id, 'proxy-end', '어머니 이야기예요. 이제 끊을게요', 'general')
    assert utterances.classified(call_id, 'proxy-end', 'end', False, True, True)
    with closing(db.connect()) as con:
        assert not session_subject_is_safe(con, call_id)
        assert con.execute('SELECT count(*) FROM call_fact_subject_boundaries WHERE call_id=?', (call_id,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 0


def test_failed_subject_boundary_write_rolls_back_accepted_state(setup, monkeypatch):
    db, _, utterances, call_id = setup
    initialize_facts(db)
    assert utterances.admit(call_id, 'proxy', '어머니가 아프세요', 'general')
    monkeypatch.setattr('app.call_utterances.mark_patient_subject_change', lambda *_: False)
    assert not utterances.classified(call_id, 'proxy', 'consultation', True, True, False)
    with closing(db.connect()) as con:
        assert con.execute("SELECT state FROM pending_utterances WHERE request_id='proxy'").fetchone()[0] == 'pending'
        assert con.execute("SELECT count(*) FROM municipal_outbox WHERE event_type='caller_utterance'").fetchone()[0] == 0


def test_private_mode_utterance_keeps_patient_chart_unconfirmed(setup):
    db, _, utterances, call_id = setup
    initialize_facts(db)
    assert utterances.admit(call_id, 'self', '제 무릎이 아파요', 'general')
    assert utterances.classified(call_id, 'self', 'consultation')
    outbox = RecommendationOutbox(db)
    outbox.set_location_consent(call_id, False)
    with closing(db.connect()) as con:
        revision = con.execute('SELECT max(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0]
    assert utterances.admit(call_id, 'private', '어머니가 아프세요', 'general', 0, revision)
    assert utterances.classified(call_id, 'private', 'consultation')
    end_call(db, call_id, 'hangup')
    with closing(db.connect()) as con:
        assert not session_subject_is_safe(con, call_id)
