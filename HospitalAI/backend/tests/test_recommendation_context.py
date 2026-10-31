"""Only current, consented public hospital details may reach model context."""

import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.call_store import CallStore
from app.db import Database
from app.recommend_dashboard import RecommendationOutbox
from app.recommend_payload import RecommendationPayload
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.recommendation_context import load_recommendation_context


@dataclass(frozen=True, slots=True)
class RecommendationCall:
    database: Database
    call_id: str
    outbox: RecommendationOutbox


def prepared_call(tmp_path: Path) -> RecommendationCall:
    database = Database(tmp_path / "recommendation-context.sqlite")
    database.initialize(seed=True)
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session("+821000000001", "sample-patient-1")["id"])
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    return RecommendationCall(database, call_id, RecommendationOutbox(database))


def enqueue_hospital(call: RecommendationCall, name: str = "현재 병원") -> str:
    query = RecommendationQuery(latitude=35.123456, longitude=126.654321, specialty="내과")
    hospital = HospitalCandidate(
        id="public-hospital", name=name, address="공개 주소", phone="062-123-4567",
        latitude=35.124, longitude=126.655, specialties=("내과",), specialists={"내과": 2},
        hours={"mon": ("0900", "1700")}, source="가상 공공 데이터",
    )
    entry = call.outbox.enqueue(call.call_id, recommend(query, [hospital]),
                                specialty="내과", location_consent=True)
    assert entry is not None
    return entry[0]


def test_context_exposes_only_public_hospital_fields_from_same_call(tmp_path: Path) -> None:
    call = prepared_call(tmp_path)
    event_id = enqueue_hospital(call)
    other_id = str(call.database.create_session("+821000000002")["id"])
    calls = CallStore(call.database)
    calls.create(other_id)
    assert calls.decide(other_id, "accepted")
    enqueue_hospital(RecommendationCall(call.database, other_id, call.outbox), "다른 통화 병원")

    context = load_recommendation_context(call.database, call.call_id)

    assert context is not None
    data = context.model_dump(mode="json")
    assert set(data) == {"specialty", "hospitals", "warning"}
    assert data["specialty"] == "내과"
    assert data["hospitals"][0]["name"] == "현재 병원"
    assert data["hospitals"][0]["phone"] == "062-123-4567"
    assert data["hospitals"][0]["address"] == "공개 주소"
    assert set(data["hospitals"][0]) == {
        "name", "phone", "address", "distance_km", "specialist_count", "registered_schedule_status",
        "hours", "specialty_match", "source",
    }
    source = context.as_source()
    assert source["id"] == "clinic:recent-recommendation"
    encoded = json.dumps(data, ensure_ascii=False) + json.dumps(source, ensure_ascii=False)
    for private_value in (call.call_id, other_id, event_id, "+821000000001", "sample-patient-1",
                          "35.123456", "126.654321", "다른 통화 병원", "latitude", "longitude"):
        assert private_value not in encoded


def test_latest_current_generation_is_selected_even_when_sent(tmp_path: Path) -> None:
    call = prepared_call(tmp_path)
    enqueue_hospital(call, "이전 병원")
    current = enqueue_hospital(call, "최신 병원")
    with closing(call.database.connect()) as con, con:
        con.execute("UPDATE recommendation_outbox SET state='sent' WHERE event_id=?", (current,))

    context = load_recommendation_context(call.database, call.call_id)

    assert context is not None
    assert context.hospitals[0].name == "최신 병원"


@pytest.mark.parametrize("gate_sql", [
    "UPDATE calls SET state='declined' WHERE id=?",
    "UPDATE calls SET urgent=1 WHERE id=?",
    "UPDATE calls SET ended_at='2026-10-30T00:00:00Z' WHERE id=?",
    "UPDATE call_location_consent SET state='revoked' WHERE call_id=?",
])
def test_call_and_location_safety_gates_remove_context(tmp_path: Path, gate_sql: str) -> None:
    call = prepared_call(tmp_path)
    enqueue_hospital(call)
    with closing(call.database.connect()) as con, con:
        con.execute(gate_sql, (call.call_id,))

    assert load_recommendation_context(call.database, call.call_id) is None


def test_location_reacceptance_never_reuses_previous_generation(tmp_path: Path) -> None:
    call = prepared_call(tmp_path)
    old = enqueue_hospital(call, "철회 전 병원")
    with closing(call.database.connect()) as con, con:
        con.execute("UPDATE recommendation_outbox SET state='sent' WHERE event_id=?", (old,))
    revoked = call.outbox.set_location_consent_with_epoch(call.call_id, False)
    assert revoked is not None
    call.outbox.set_location_consent(call.call_id, True, source_location_generation=0, source_location_revision=revoked[3])

    assert load_recommendation_context(call.database, call.call_id) is None
    enqueue_hospital(call, "재동의 뒤 병원")
    context = load_recommendation_context(call.database, call.call_id)
    assert context is not None
    assert context.hospitals[0].name == "재동의 뒤 병원"


@pytest.mark.parametrize("table_sql", [
    "INSERT INTO recommendation_cancellations(event_id) VALUES (?)",
    "INSERT INTO recommendation_failures(event_id,reason) VALUES (?,'invalid_payload')",
])
def test_cancelled_or_failed_snapshot_is_excluded(tmp_path: Path, table_sql: str) -> None:
    call = prepared_call(tmp_path)
    event_id = enqueue_hospital(call)
    with closing(call.database.connect()) as con, con:
        con.execute(table_sql, (event_id,))

    assert load_recommendation_context(call.database, call.call_id) is None


@pytest.mark.parametrize("status,text", [
    ("emergency", "긴급 안내를 받았어요"),
    ("completed", "농약을 뿌렸는데 어지러워요"),
])
def test_emergency_turn_or_signal_removes_existing_snapshot(
    tmp_path: Path, status: str, text: str,
) -> None:
    call = prepared_call(tmp_path)
    enqueue_hospital(call)
    turn, _ = call.database.create_turn(call.call_id, "urgent-after-recommendation", text)
    call.database.update_turn(str(turn["id"]), {"status": status})

    assert load_recommendation_context(call.database, call.call_id) is None


@pytest.mark.parametrize("body_sql", [
    "UPDATE recommendation_outbox SET body='{malformed' WHERE event_id=?",
    "UPDATE recommendation_outbox SET body=json_set(body,'$.call_id','other-call') WHERE event_id=?",
    "UPDATE recommendation_outbox SET body=json_set(body,'$.event_id','other-event') WHERE event_id=?",
    "UPDATE recommendation_outbox SET body=json_set(body,'$.hospitals[0].latitude',35.123456) WHERE event_id=?",
])
def test_malformed_or_identity_mismatched_payload_is_excluded(tmp_path: Path, body_sql: str) -> None:
    call = prepared_call(tmp_path)
    event_id = enqueue_hospital(call)
    with closing(call.database.connect()) as con, con:
        con.execute(body_sql, (event_id,))

    assert load_recommendation_context(call.database, call.call_id) is None


def test_bounded_snapshot_preserves_unknowns_without_inventing_details(tmp_path: Path) -> None:
    call = prepared_call(tmp_path)
    event_id = enqueue_hospital(call)
    with closing(call.database.connect()) as con, con:
        row = con.execute("SELECT body FROM recommendation_outbox WHERE event_id=?", (event_id,)).fetchone()
        payload = RecommendationPayload.model_validate_json(str(row[0]))
        unknown = payload.hospitals[0].model_copy(update={
            "phone": None, "hours": None, "specialist_count": None,
            "specialty_match": "unknown", "registered_schedule_status": "unknown",
        })
        bounded = payload.model_copy(update={"hospitals": [unknown] * 8, "warning": "확인 필요 " * 300})
        con.execute("UPDATE recommendation_outbox SET body=? WHERE event_id=?", (bounded.model_dump_json(), event_id))

    context = load_recommendation_context(call.database, call.call_id)

    assert context is not None
    assert len(context.hospitals) == 3
    assert context.hospitals[0].phone is None
    assert context.hospitals[0].hours is None
    assert context.hospitals[0].specialist_count is None
    assert context.hospitals[0].specialty_match == "unknown"
    assert context.hospitals[0].registered_schedule_status == "unknown"
    assert len(context.model_dump_json()) < 5000


def test_database_without_recommendation_tables_has_no_context(tmp_path: Path) -> None:
    database = Database(tmp_path / "ordinary-session.sqlite")
    database.initialize()
    session = database.create_session("+821000000003")

    assert load_recommendation_context(database, str(session["id"])) is None
