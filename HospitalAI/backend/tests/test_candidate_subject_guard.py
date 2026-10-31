"""Protect the caller's patient chart after a call changes patient subject."""

from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.call_facts import initialize
from app.call_store import CallStore
from app.call_utterances import PendingUtterances
from app.config import Settings
from app.db import Database, InvalidCandidate
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def patient_call(tmp_path: Path) -> tuple[Database, str, str]:
    database = Database(tmp_path / "candidate-subject.sqlite")
    database.initialize(seed=True)
    call_id = str(database.create_session("+821000000001", "sample-patient-1")["id"])
    calls = CallStore(database)
    calls.initialize()
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    initialize(database)
    utterances = PendingUtterances(database)
    utterances.initialize()
    assert utterances.admit(call_id, "turn-one", "무릎이 아파요", "general")
    assert utterances.classified(call_id, "turn-one", "consultation")
    turn, _ = database.create_turn(call_id, "turn-one", "무릎이 아파요")
    return database, call_id, str(turn["id"])


def switch_patient(database: Database, call_id: str) -> None:
    with closing(database.connect()) as con, con:
        con.execute("""INSERT INTO call_fact_subject_boundaries
            (call_id,utterance_id,source_sequence,created_at)
            VALUES (?, 'proxy', 2, '2026-01-01T00:00:00Z')""", (call_id,))


def finish_call(database: Database, call_id: str) -> None:
    with closing(database.connect()) as con, con:
        con.execute("UPDATE calls SET ended_at='2026-01-01T00:00:01Z',end_reason='caller' WHERE id=?", (call_id,))


def historic_candidate(database: Database, turn_id: str) -> str:
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO candidates(id,turn_id,patient_id,text) VALUES ('legacy', ?, 'sample-patient-1', '무릎 통증')", (turn_id,))
    return 'legacy'


def test_candidate_creation_fails_after_patient_subject_changes(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    switch_patient(database, call_id)
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.create_candidate(turn_id, "sample-patient-1", "무릎 통증")
    assert database.candidates("sample-patient-1") == []


def test_active_call_cannot_create_or_approve_candidate(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO candidates(id,turn_id,patient_id,text) VALUES ('old', ?, 'sample-patient-1', '기록')", (turn_id,))
    with pytest.raises(InvalidCandidate):
        database.create_candidate(turn_id, "sample-patient-1", "새 기록")
    with pytest.raises(InvalidCandidate):
        database.review_candidate("old", "approved", "reviewer")
    assert database.candidates("sample-patient-1", session_id=call_id) == []


def test_existing_candidate_cannot_be_approved_after_subject_changes(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    switch_patient(database, call_id)
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, "approved", "reviewer")
    assert database.candidates("sample-patient-1") == []
    assert not any(row["text"] == "무릎 통증" for row in database.search("sample-patient-1", "무릎"))


def test_subject_switch_in_one_call_does_not_hide_another_call_candidate(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    other_session = database.create_session("+821000000001", "sample-patient-1")
    other_turn, _ = database.create_turn(str(other_session["id"]), "other", "허리가 아파요")
    other = database.create_candidate(str(other_turn["id"]), "sample-patient-1", "허리 통증")
    switch_patient(database, call_id)
    finish_call(database, call_id)
    rows = database.candidates("sample-patient-1")
    assert [row["id"] for row in rows] == [other["id"]]
    assert candidate_id != other["id"]
    assert database.review_candidate(str(other["id"]), "approved", "reviewer")["status"] == "approved"
    assert database.candidates("sample-patient-1", session_id=call_id) == []


def test_unclassified_new_utterance_blocks_candidate_until_classified(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    pending = PendingUtterances(database)
    assert pending.admit(call_id, "pending", "어머니가 아프세요", "general")
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, "approved", "reviewer")
    with pytest.raises(InvalidCandidate):
        database.create_candidate(turn_id, "sample-patient-1", "다른 기록")
    assert database.review_candidate(candidate_id, "rejected", "reviewer")["status"] == "rejected"


def test_classified_proxy_utterance_atomically_blocks_existing_candidate(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    utterances = PendingUtterances(database)
    assert utterances.admit(call_id, "proxy", "어머니가 아프세요", "general")
    assert utterances.classified(call_id, "proxy", "consultation", proxy_subject=True)
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, "approved", "reviewer")
    assert not any(row["text"] == "무릎 통증" for row in database.search("sample-patient-1", "무릎"))


def test_legacy_call_without_classified_utterance_does_not_write_chart(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.sqlite")
    database.initialize(seed=True)
    call_id = str(database.create_session("+821000000001", "sample-patient-1")["id"])
    calls = CallStore(database)
    calls.initialize()
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    turn, _ = database.create_turn(call_id, "legacy", "아파요")
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.create_candidate(str(turn["id"]), "sample-patient-1", "상담 내용")


def test_unmigrated_legacy_call_fails_closed_without_database_error(tmp_path: Path) -> None:
    database = Database(tmp_path / 'unmigrated.sqlite')
    database.initialize(seed=True)
    call_id = str(database.create_session('+821000000001', 'sample-patient-1')['id'])
    with closing(database.connect()) as con, con:
        con.execute('CREATE TABLE calls(id TEXT PRIMARY KEY,state TEXT NOT NULL)')
        con.execute("INSERT INTO calls(id,state) VALUES (?, 'accepted')", (call_id,))
    turn, _ = database.create_turn(call_id, 'legacy', '아파요')
    with pytest.raises(InvalidCandidate):
        database.create_candidate(str(turn['id']), 'sample-patient-1', '상담 내용')


def test_legacy_location_reset_alone_blocks_chart_approval(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO call_location_resets(call_id,request_id,generation) VALUES (?, 'old-reset', 2)", (call_id,))
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, "approved", "reviewer")


def test_verified_self_call_can_create_and_approve_candidate_after_end(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    finish_call(database, call_id)
    candidate = database.create_candidate(turn_id, "sample-patient-1", "무릎 통증")
    assert database.review_candidate(str(candidate['id']), 'approved', 'reviewer')['status'] == 'approved'


def test_private_locationless_utterance_blocks_candidate_after_later_consent(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    utterances = PendingUtterances(database)
    assert utterances.admit(call_id, 'private', '어머니가 아파요', 'general')
    assert utterances.classified(call_id, 'private', 'consultation')
    with closing(database.connect()) as con, con:
        con.execute("UPDATE pending_utterances SET user_location_generation=0 WHERE call_id=? AND request_id='private'", (call_id,))
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.create_candidate(turn_id, 'sample-patient-1', '무릎 통증')
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, 'approved', 'reviewer')
    assert database.candidates('sample-patient-1', session_id=call_id) == []


def test_private_unlinked_turn_blocks_candidate_after_later_consent(tmp_path: Path) -> None:
    database, call_id, turn_id = patient_call(tmp_path)
    candidate_id = historic_candidate(database, turn_id)
    private_turn, _ = database.create_turn(call_id, 'private-turn', '상담 내용')
    with closing(database.connect()) as con, con:
        con.execute('UPDATE turns SET user_location_generation=0 WHERE id=?', (private_turn['id'],))
    finish_call(database, call_id)
    with pytest.raises(InvalidCandidate):
        database.review_candidate(candidate_id, 'approved', 'reviewer')


def test_http_candidate_review_cannot_approve_after_proxy_switch(tmp_path: Path) -> None:
    database = Database(tmp_path / "http.sqlite")
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=database.path)
    with TestClient(create_app(settings, database, ServiceBundle(MockLLM(), MockTTS())),
                    headers={"Authorization": "Bearer service"}) as client:
        response = client.post("/sessions", json={"phone": "+821000000001", "verification_token": "verify"})
        assert response.status_code == 200
        call_id = response.json()["id"]
        calls = CallStore(database)
        calls.create(call_id)
        assert calls.decide(call_id, "accepted")
        utterances = PendingUtterances(database)
        assert utterances.admit(call_id, "turn-one", "무릎이 아파요", "general")
        assert utterances.classified(call_id, "turn-one", "consultation")
        turn, _ = database.create_turn(call_id, "turn-one", "무릎이 아파요")
        candidate = client.post(f"/sessions/{call_id}/candidates", json={"turn_id": turn["id"], "text": "무릎 통증"})
        assert candidate.status_code == 400
        candidate_id = historic_candidate(database, str(turn["id"]))
        switch_patient(database, call_id)
        finish_call(database, call_id)
        assert client.get(f"/sessions/{call_id}/candidates").json() == []
        response = client.post(f"/candidates/{candidate_id}/review",
            json={"decision": "approved", "reviewer": "reviewer"}, headers={"Authorization": "Bearer admin"})
        assert response.status_code == 409
    assert not any(row["text"] == "무릎 통증" for row in database.search("sample-patient-1", "무릎"))
