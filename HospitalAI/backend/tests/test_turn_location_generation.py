from contextlib import closing
from pathlib import Path

import pytest

from app.call_store import CallStore
from app.db import Database, DuplicateRequest
from app.recommend_dashboard import RecommendationOutbox
from app.schemas import TurnInput
from app.utterance_location import StaleLocationGeneration, capture_location_revision


@pytest.fixture
def active_call(tmp_path: Path):
    database = Database(tmp_path / 'turn-location.sqlite')
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    assert calls.decide(call_id, 'accepted')
    return database, calls, call_id


def test_legacy_turn_remains_unknown_after_schema_migration(tmp_path: Path):
    database = Database(tmp_path / 'legacy.sqlite')
    with closing(database.connect()) as con, con:
        con.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, phone TEXT NOT NULL, patient_id TEXT)')
        con.execute("INSERT INTO sessions VALUES ('call', '+821000000001', NULL)")
        con.execute('''CREATE TABLE turns (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
            input_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', initial_text TEXT, final_text TEXT,
            sources_json TEXT, timings_json TEXT, tts_json TEXT, error TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE(session_id, request_id))''')
        con.execute("INSERT INTO turns(id,session_id,request_id,input_text) VALUES ('turn','call','legacy','불편해요')")
    database.initialize()
    row = database.get_turn('call', 'legacy')
    assert row and row['id'] == 'turn' and row['user_location_generation'] is None and row['user_location_revision'] is None


def test_turn_without_provisional_utterance_captures_current_generation(active_call):
    database, calls, call_id = active_call
    accepted = calls.submit_turn(call_id, TurnInput(request_id='first', text='무릎이 불편해요'))
    assert accepted is not None and accepted[0]['user_location_generation'] == 1 and accepted[0]['user_location_revision'] == 1
    RecommendationOutbox(database).set_location_consent(call_id, False)
    with closing(database.connect()) as con:
        revision = capture_location_revision(con, call_id)
    unlocated = calls.submit_turn(call_id, TurnInput(request_id='second', text='다리가 불편해요', source_location_generation=0, source_location_revision=revision))
    assert unlocated is not None and unlocated[0]['user_location_generation'] == 0 and unlocated[0]['user_location_revision'] == revision


@pytest.mark.parametrize('original_generation', [None, 0, 1])
def test_turn_rejects_old_utterance_after_location_consent_changes(active_call, original_generation):
    database, calls, call_id = active_call
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE pending_utterances (
            call_id TEXT NOT NULL, request_id TEXT NOT NULL, input_text TEXT NOT NULL,
            caller_context TEXT NOT NULL, user_location_generation INTEGER,
            PRIMARY KEY (call_id, request_id))''')
        con.execute('INSERT INTO pending_utterances VALUES (?,?,?,?,?)',
                    (call_id, 'first', '무릎이 불편해요', 'general', original_generation))
    RecommendationOutbox(database).set_location_consent(call_id, False)
    RecommendationOutbox(database).set_location_consent(call_id, True, source_location_generation=0)
    with pytest.raises(StaleLocationGeneration):
        calls.submit_turn(call_id, TurnInput(request_id='first', text='무릎이 불편해요', source_location_generation=2))
    assert database.get_turn(call_id, 'first') is None


def test_legacy_utterance_without_generation_is_not_relabelled(active_call):
    database, calls, call_id = active_call
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE pending_utterances (
            call_id TEXT NOT NULL, request_id TEXT NOT NULL, input_text TEXT NOT NULL,
            caller_context TEXT NOT NULL, PRIMARY KEY (call_id, request_id))''')
        con.execute('INSERT INTO pending_utterances VALUES (?,?,?,?)',
                    (call_id, 'first', '무릎이 불편해요', 'general'))
    result = calls.submit_turn(call_id, TurnInput(request_id='first', text='무릎이 불편해요'))
    assert result is not None and result[0]['user_location_generation'] is None


def test_utterance_id_cannot_be_promoted_with_different_text(active_call):
    database, calls, call_id = active_call
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE pending_utterances (
            call_id TEXT NOT NULL, request_id TEXT NOT NULL, input_text TEXT NOT NULL,
            caller_context TEXT NOT NULL, user_location_generation INTEGER,
            PRIMARY KEY (call_id, request_id))''')
        con.execute('INSERT INTO pending_utterances VALUES (?,?,?,?,?)',
                    (call_id, 'first', '무릎이 불편해요', 'general', 1))
    with pytest.raises(DuplicateRequest):
        calls.submit_turn(call_id, TurnInput(request_id='first', text='다리가 불편해요'))
    assert database.get_turn(call_id, 'first') is None


def test_urgent_turn_retains_source_generation(active_call):
    _, calls, call_id = active_call
    result = calls.submit_turn(call_id, TurnInput(request_id='urgent', text='숨을 못 쉬어요'))
    assert result is not None and result[2] and result[0]['user_location_generation'] == 1


def test_stale_unlocated_speech_rejected_after_another_withdrawal(active_call):
    database, calls, call_id = active_call
    outbox = RecommendationOutbox(database)
    outbox.set_location_consent(call_id, False)
    with closing(database.connect()) as con:
        old_revision = capture_location_revision(con, call_id)
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=old_revision)
    with closing(database.connect()) as con:
        active_revision = capture_location_revision(con, call_id)
    outbox.set_location_consent(call_id, False, source_location_generation=2, source_location_revision=active_revision)
    with pytest.raises(StaleLocationGeneration):
        calls.submit_turn(call_id, TurnInput(request_id='stale', text='무릎이 불편해요', source_location_generation=0, source_location_revision=old_revision))
    assert database.get_turn(call_id, 'stale') is None


def test_existing_id_rejects_different_source_revision(active_call):
    _, calls, call_id = active_call
    result = calls.submit_turn(call_id, TurnInput(request_id='one', text='무릎이 불편해요', source_location_generation=1, source_location_revision=1))
    assert result is not None and result[0]['user_location_revision'] == 1
    with pytest.raises((StaleLocationGeneration, DuplicateRequest)):
        calls.submit_turn(call_id, TurnInput(request_id='one', text='무릎이 불편해요', source_location_generation=1, source_location_revision=99))


def test_no_location_prior_utterance_in_current_revision_retains_safety_context(active_call):
    database, calls, call_id = active_call
    RecommendationOutbox(database).set_location_consent(call_id, False)
    with closing(database.connect()) as con:
        revision = capture_location_revision(con, call_id)
    first = calls.submit_turn(call_id, TurnInput(request_id='first', text='어제 농약을 뿌렸어요',
        source_location_generation=0, source_location_revision=revision))
    assert first is not None and not first[2]
    second = calls.submit_turn(call_id, TurnInput(request_id='second', text='어지러워요',
        source_location_generation=0, source_location_revision=revision))
    assert second is not None and second[2]


def test_no_location_prior_utterance_from_old_revision_is_not_reused(active_call):
    database, calls, call_id = active_call
    outbox = RecommendationOutbox(database)
    outbox.set_location_consent(call_id, False)
    with closing(database.connect()) as con:
        first_revision = capture_location_revision(con, call_id)
    first = calls.submit_turn(call_id, TurnInput(request_id='first', text='어제 농약을 뿌렸어요',
        source_location_generation=0, source_location_revision=first_revision))
    assert first is not None and not first[2]
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=first_revision)
    with closing(database.connect()) as con:
        accepted_revision = capture_location_revision(con, call_id)
    outbox.set_location_consent(call_id, False, source_location_generation=2, source_location_revision=accepted_revision)
    with closing(database.connect()) as con:
        second_revision = capture_location_revision(con, call_id)
    second = calls.submit_turn(call_id, TurnInput(request_id='second', text='어지러워요',
        source_location_generation=0, source_location_revision=second_revision))
    assert second is not None and not second[2]
