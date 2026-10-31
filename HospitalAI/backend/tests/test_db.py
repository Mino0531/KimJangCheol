from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.db import Database, DuplicateRequest, InvalidCandidate


def test_turn_claim_is_idempotent_and_rejects_changed_text(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    first, created = db.create_turn(str(session["id"]), "request-1", "기침이 있어요")
    duplicate, created_again = db.create_turn(str(session["id"]), "request-1", "기침이 있어요")
    assert created and not created_again and first["id"] == duplicate["id"]
    with pytest.raises(DuplicateRequest):
        db.create_turn(str(session["id"]), "request-1", "다른 발화")


def test_candidate_approval_adds_record_for_only_its_patient(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "request-1", "무릎이 아파요")
    candidate = db.create_candidate(str(turn["id"]), "sample-patient-1", "무릎 통증")
    db.review_candidate(str(candidate["id"]), "approved", "clinician-1")
    assert any(item["text"] == "무릎 통증" for item in db.search("sample-patient-1", "무릎"))
    assert not any(item["text"] == "무릎 통증" for item in db.search("sample-patient-2", "무릎"))


def test_unverified_search_excludes_patient_records(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    assert not any(item["type"] == "patient" for item in db.search(None, "고혈압"))


def test_candidate_cannot_be_attached_to_another_patient(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "request-1", "기침")
    with pytest.raises(InvalidCandidate):
        db.create_candidate(str(turn["id"]), "sample-patient-2", "기침")


def test_rejected_candidate_cannot_be_approved_later(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "request-1", "기침")
    candidate = db.create_candidate(str(turn["id"]), "sample-patient-1", "기침")
    db.review_candidate(str(candidate["id"]), "rejected", "clinician-1")
    with pytest.raises(InvalidCandidate):
        db.review_candidate(str(candidate["id"]), "approved", "clinician-1")


def test_concurrent_duplicate_claim_has_one_owner(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: db.create_turn(str(session["id"]), "request-1", "기침"), range(4)))
    assert sum(created for _, created in claims) == 1
    assert len({row["id"] for row, _ in claims}) == 1
