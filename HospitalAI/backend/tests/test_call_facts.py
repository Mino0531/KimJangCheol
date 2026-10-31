from contextlib import closing

import pytest

from app.call_store import CallStore
from app.db import Database
import app.call_facts as facts


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / 'facts.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    with closing(db.connect()) as con, con:
        con.execute('''CREATE TABLE pending_utterances (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, request_id TEXT NOT NULL,
            input_text TEXT NOT NULL, caller_context TEXT NOT NULL DEFAULT 'general',
            state TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(call_id,request_id))''')
    facts.initialize(db)
    call = str(db.create_session('+821000000000')['id'])
    calls.create(call)
    assert calls.decide(call, 'accepted')
    return db, calls, call


def utterance(db, call, request, text, state='accepted'):
    with closing(db.connect()) as con, con:
        con.execute('INSERT INTO pending_utterances(call_id,request_id,input_text,state) VALUES (?,?,?,?)',
                    (call, request, text, state))


def fact(key, quote, operation='replace'):
    return facts.FactUpdate(key=key, quote=quote, operation=operation)


def test_latest_correction_wins_even_if_model_calls_finish_out_of_order(setup):
    db, _, call = setup
    utterance(db, call, 'old', '어제 시작했어요')
    utterance(db, call, 'new', '아니 오늘 시작했어요')
    assert facts.apply_explicit(db, call, 'new', [fact('onset', '오늘 시작했어요')], 1)
    assert facts.apply_explicit(db, call, 'old', [fact('onset', '어제 시작했어요')], 1)
    snapshot = facts.read_current(db, call, through_utterance_id='new')
    assert [(item.key, item.quote, item.utterance_id) for item in snapshot] == [('onset', '오늘 시작했어요', 'new')]


def test_model_cannot_invent_source_text(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    with pytest.raises(facts.InvalidFact):
        facts.apply_explicit(db, call, 'u1', [fact('symptom', '열이 나요')], 1)
    assert not facts.read_current(db, call, through_utterance_id='u1')


def test_pending_utterance_does_not_become_fact(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요', 'pending')
    assert not facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)


def test_earlier_turn_cannot_see_future_facts(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '어제 시작했어요')
    utterance(db, call, 'u2', '오늘 시작했어요')
    assert facts.apply_explicit(db, call, 'u1', [fact('onset', '어제 시작했어요')], 1)
    assert facts.apply_explicit(db, call, 'u2', [fact('onset', '오늘 시작했어요')], 1)
    assert [x.quote for x in facts.read_current(db, call, through_utterance_id='u1')] == ['어제 시작했어요']


def test_location_withdrawal_blocks_old_and_inflight_facts(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    assert facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE call_location_consent SET state='revoked', generation=2 WHERE call_id=?", (call,))
    assert [item.quote for item in facts.read_current(db, call, through_utterance_id='u1')] == ['무릎이 아파요']
    assert not facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)


def test_ended_or_declined_call_cannot_write_facts(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET ended_at='2026-01-01' WHERE id=?", (call,))
    assert not facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)


def test_duplicate_submission_is_idempotent(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    update = [fact('symptom', '무릎이 아파요')]
    assert facts.apply_explicit(db, call, 'u1', update, 1)
    assert facts.apply_explicit(db, call, 'u1', update, 1)
    with closing(db.connect()) as con:
        assert con.execute('SELECT count(*) FROM call_fact_events').fetchone()[0] == 1


def test_new_caller_facts_work_after_location_withdrawal(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '기침이 나요')
    with closing(db.connect()) as con, con:
        con.execute("UPDATE call_location_consent SET state='revoked',generation=2 WHERE call_id=?", (call,))
    assert facts.apply_explicit(db, call, 'u1', [fact('symptom', '기침이 나요')], None)
    assert [item.quote for item in facts.read_current(db, call, through_utterance_id='u1')] == ['기침이 나요']


def test_snapshot_identifies_superseded_source_quote(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '어제 시작했어요')
    utterance(db, call, 'u2', '오늘 시작했어요')
    assert facts.apply_explicit(db, call, 'u1', [fact('onset', '어제 시작했어요')], 1)
    assert facts.apply_explicit(db, call, 'u2', [fact('onset', '오늘 시작했어요')], 1)
    snapshot = facts.read_snapshot(db, call, through_utterance_id='u2')
    assert [(item.utterance_id, item.quote) for item in snapshot.current] == [('u2', '오늘 시작했어요')]
    assert [(item.utterance_id, item.quote) for item in snapshot.superseded] == [('u1', '어제 시작했어요')]


def test_location_containing_or_unknown_quote_hidden_after_withdrawal(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '화순읍에서 무릎이 아파요')
    assert facts.apply_explicit(db, call, 'u1', [fact('symptom', '화순읍에서 무릎이 아파요')], 1)
    with closing(db.connect()) as con, con:
        con.execute("UPDATE call_location_consent SET state='revoked',generation=2 WHERE call_id=?", (call,))
    assert not facts.read_current(db, call, through_utterance_id='u1')


def test_multiple_corrections_in_one_utterance_keep_last_replace(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '어제 아니 오늘')
    assert facts.apply_explicit(db, call, 'u1', [fact('onset', '어제', 'add'), fact('onset', '오늘')], 1)
    assert [item.quote for item in facts.read_current(db, call, through_utterance_id='u1')] == ['오늘']


def test_correction_is_retained_after_fifteen_later_utterances(setup):
    db, _, call = setup
    utterance(db, call, 'first', '어제 시작했어요')
    utterance(db, call, 'corrected', '오늘 시작했어요')
    assert facts.apply_explicit(db, call, 'first', [fact('onset', '어제 시작했어요')], 1)
    assert facts.apply_explicit(db, call, 'corrected', [fact('onset', '오늘 시작했어요')], 1)
    for index in range(15):
        utterance(db, call, f'followup-{index}', '알겠어요')
    snapshot = facts.read_snapshot(db, call, through_utterance_id='followup-14')
    assert [item.quote for item in snapshot.current] == ['오늘 시작했어요']
    assert [item.quote for item in snapshot.superseded] == ['어제 시작했어요']


def test_location_revocation_removes_only_dependent_fact_events(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '화순읍에서 무릎이 아파요')
    utterance(db, call, 'u2', '무릎이 아파요')
    assert facts.apply_explicit(db, call, 'u1', [fact('symptom', '화순읍에서 무릎이 아파요')], 1)
    assert facts.apply_explicit(db, call, 'u2', [fact('symptom', '무릎이 아파요')], 1)
    with closing(db.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        facts.invalidate_location_facts(con, call)
        assert [row['quote'] for row in con.execute('SELECT quote FROM call_fact_events')] == ['무릎이 아파요']


@pytest.mark.parametrize('changes', ["state='declined'", 'urgent=1'])
def test_revoked_or_urgent_call_blocks_facts(setup, changes):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    with closing(db.connect()) as con, con:
        con.execute(f'UPDATE calls SET {changes} WHERE id=?', (call,))
    assert not facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)
    assert not facts.read_snapshot(db, call, through_utterance_id='u1').current


def test_snapshot_is_empty_for_legacy_database_without_fact_tables(tmp_path):
    db = Database(tmp_path / 'legacy.sqlite')
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    call = str(db.create_session('+821000000000')['id'])
    calls.create(call)
    assert calls.decide(call, 'accepted')
    assert not facts.read_snapshot(db, call, through_utterance_id='legacy-turn').current


def test_patient_change_hides_prior_patient_facts_and_history(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    old_turn, _ = db.create_turn(call, 'u1', '무릎이 아파요')
    assert facts.apply_explicit(db, call, 'u1', [fact('symptom', '무릎이 아파요')], 1)
    utterance(db, call, 'u2', '어머니가 어지러워요')
    assert facts.apply_explicit(db, call, 'u2', [fact('symptom', '어지러워요')], 1)
    with closing(db.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        assert facts.mark_patient_subject_change(con, call, 'u2')
    snapshot = facts.read_snapshot(db, call, through_utterance_id='u2')
    assert [item.quote for item in snapshot.current] == ['어지러워요']
    assert old_turn['request_id'] in snapshot.subject_excluded_utterance_ids
    assert snapshot.patient_subject_changed is True
    previous = facts.read_snapshot(db, call, through_utterance_id='u1')
    assert [item.quote for item in previous.current] == ['무릎이 아파요']
    assert previous.patient_subject_changed is False


def test_patient_change_is_visible_even_without_earlier_utterances(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '어머니가 어지러워요')
    with closing(db.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        assert facts.mark_patient_subject_change(con, call, 'u1')
    snapshot = facts.read_snapshot(db, call, through_utterance_id='u1')
    assert not snapshot.subject_excluded_utterance_ids
    assert snapshot.patient_subject_changed is True


def test_unknown_legacy_utterance_after_patient_change_still_disables_original_patient(setup):
    db, _, call = setup
    utterance(db, call, 'proxy', '어머니가 어지러워요')
    with closing(db.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        assert facts.mark_patient_subject_change(con, call, 'proxy')
    db.create_turn(call, 'legacy', '가까운 병원 알려줘요')
    snapshot = facts.read_snapshot(db, call, through_utterance_id='legacy')
    assert snapshot.patient_subject_changed is True
    assert not snapshot.current


def test_snapshot_reads_within_callers_existing_transaction(setup):
    db, _, call = setup
    utterance(db, call, 'u1', '무릎이 아파요')
    with closing(db.connect()) as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('''INSERT INTO call_fact_events(call_id,utterance_id,source_sequence,fact_key,
            quote,operation,location_generation) VALUES (?, 'u1', 1, 'symptom', '무릎이 아파요', 'replace', 0)''', (call,))
        snapshot = facts.read_snapshot(db, call, through_utterance_id='u1', connection=con)
        assert [item.quote for item in snapshot.current] == ['무릎이 아파요']
        con.rollback()


def test_withdrawal_does_not_revive_old_fact_replaced_by_location_bound_correction(setup):
    db, _, call = setup
    utterance(db, call, 'old', '어제 시작했어요')
    utterance(db, call, 'correction', '화순읍에서 오늘 시작했어요')
    assert facts.apply_explicit(db, call, 'old', [fact('onset', '어제 시작했어요')], 1)
    assert facts.apply_explicit(db, call, 'correction', [fact('onset', '화순읍에서 오늘 시작했어요')], 1)
    with closing(db.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE call_location_consent SET state='revoked',generation=2 WHERE call_id=?", (call,))
        facts.invalidate_location_facts(con, call)
    snapshot = facts.read_snapshot(db, call, through_utterance_id='correction')
    assert not snapshot.current
    assert [item.quote for item in snapshot.superseded] == ['어제 시작했어요']
    with closing(db.connect()) as con, con:
        con.execute("UPDATE call_location_consent SET state='accepted',generation=3 WHERE call_id=?", (call,))
    assert not facts.read_snapshot(db, call, through_utterance_id='correction').current
