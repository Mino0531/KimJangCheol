from pathlib import Path
import sqlite3

import pytest

from app.call_store import CallStore
from app.audio_store import Audio, AudioStore
from app.db import Database
from app.municipal_bridge import MunicipalOutbox
from app.recommend_dashboard import RecommendationOutbox


def setup_call(path: Path) -> tuple[Database, CallStore, str]:
    database = Database(path)
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    AudioStore(database).initialize()
    RecommendationOutbox(database).initialize()
    MunicipalOutbox(database).initialize()
    call_id = str(database.create_session('+821000000001')['id'])
    calls.create(call_id)
    return database, calls, call_id


def test_new_unified_consent_records_both_purposes_and_municipal_notice(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'call.sqlite')
    assert calls.decide(call_id, 'accepted')
    assert calls.get(call_id)['consent_version'] == 'municipality-v3'
    assert RecommendationOutbox(database).location_generation(call_id) == 1
    with database.connect() as con:
        assert con.execute('SELECT state,generation FROM location_consent_events WHERE call_id=?',
                           (call_id,)).fetchone()[:] == ('accepted', 1)
    assert MunicipalOutbox(database).enqueue_start(call_id)


def test_legacy_consent_does_not_gain_location_automatically(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'legacy.sqlite')
    with database.connect() as con:
        con.execute("UPDATE calls SET consent_version='municipality-v2' WHERE id=?", (call_id,))
        con.commit()
    assert calls.decide(call_id, 'accepted')
    assert RecommendationOutbox(database).location_generation(call_id) is None
    assert MunicipalOutbox(database).enqueue_start(call_id)


def test_failed_location_record_rolls_back_call_consent(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'rollback.sqlite')
    with database.connect() as con:
        con.execute("CREATE TRIGGER reject_location BEFORE INSERT ON call_location_consent BEGIN SELECT RAISE(ABORT, 'test'); END")
        con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        calls.decide(call_id, 'accepted')
    assert calls.get(call_id)['state'] == 'awaiting_consent'
    assert RecommendationOutbox(database).location_generation(call_id) is None
    assert not MunicipalOutbox(database).enqueue_start(call_id)


def test_emergency_prevents_consent_and_location_commit(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'urgent.sqlite')
    assert calls.mark_urgent(call_id)
    assert not calls.decide(call_id, 'accepted')
    assert RecommendationOutbox(database).location_generation(call_id) is None
    assert not MunicipalOutbox(database).enqueue_start(call_id)


def test_location_can_still_be_revoked_and_reaccepted(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'withdraw.sqlite')
    assert calls.decide(call_id, 'accepted')
    outbox = RecommendationOutbox(database)
    revoked = outbox.set_location_consent_with_epoch(call_id, False)
    assert revoked is not None and revoked[:2] == ('revoked', 0)
    assert outbox.location_generation(call_id) is None
    assert outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=revoked[3]) == ('accepted', 0)
    assert outbox.location_generation(call_id) == 2


def test_withdrawn_call_rejects_inflight_results_and_audio(tmp_path: Path) -> None:
    database, calls, call_id = setup_call(tmp_path / 'inflight.sqlite')
    assert calls.decide(call_id, 'accepted')
    turn, _ = database.create_turn(call_id, 'inflight', '가상 문의')
    with database.connect() as con:
        con.execute("UPDATE calls SET state='declined' WHERE id=?", (call_id,))
        con.commit()
    assert not calls.update_turn_if_safe(str(turn['id']), {'final_text': '답변'})
    assert not calls.save_audio_if_safe(str(turn['id']), 'final', Audio(b'audio', 'audio/mpeg'))
