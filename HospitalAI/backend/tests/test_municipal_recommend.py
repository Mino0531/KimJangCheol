"""Municipal recommendation delivery respects location consent and urgent state."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import anyio
import httpx2
import pytest

from app.call_store import CallStore
from app.db import Database
from app.hospital_bridge import DispatchResult, HospitalConfig
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend


def setup_call(tmp_path: Path) -> tuple[Database, str, RecommendationOutbox]:
    database = Database(tmp_path / "calls.sqlite")
    database.initialize()
    calls = CallStore(database)
    calls.initialize()
    call_id = str(database.create_session("+821000000001")["id"])
    calls.create(call_id)
    with closing(database.connect()) as con, con:
        con.execute("UPDATE calls SET consent_version='municipality-v2' WHERE id=?", (call_id,))
    assert calls.decide(call_id, "accepted")
    outbox = RecommendationOutbox(database)
    outbox.initialize()
    return database, call_id, outbox


def sample_result():
    query = RecommendationQuery(latitude=35.123456, longitude=126.654321, specialty="내과")
    hospital = HospitalCandidate(id="test-1", name="가상 병원", address="가상 주소", phone="062-000-0000",
        latitude=35.2, longitude=126.6, specialties=("내과",), specialists={"내과": 2},
        hours={"mon": ("0900", "1700")}, source="가상 공공 데이터")
    return recommend(query, [hospital])


def dispatch(database: Database, handler) -> DispatchResult:
    from app.municipal_recommend import MunicipalRecommendationDispatch

    async def run() -> DispatchResult:
        return await MunicipalRecommendationDispatch(database).dispatch(
            HospitalConfig("http://127.0.0.1:8767", "test-token"), transport=httpx2.MockTransport(handler))

    return anyio.run(run)


def test_location_consent_is_required_before_a_recommendation_can_be_enqueued(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is None
    requests = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500, request=request)

    dispatch(database, receive)
    assert requests == []


def test_recommendation_delivery_excludes_coordinates_and_is_idempotent(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    assert outbox.set_location_consent(call_id, True) is not None
    first = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    second = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert first is not None and second is not None and first[0] == second[0]
    requests = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    dispatch(database, receive)
    assert len(requests) == 1
    assert requests[0]["event_type"] == "recommendation"
    assert requests[0]["location_generation"] == 1
    assert requests[0]["recommendation"]["hospitals"][0]["name"] == "가상 병원"
    encoded = json.dumps(requests[0], ensure_ascii=False)
    assert "35.123456" not in encoded and "126.654321" not in encoded
    assert "latitude" not in encoded and "longitude" not in encoded


def test_urgent_call_does_not_deliver_pending_recommendations(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is not None
    assert CallStore(database).mark_urgent(call_id)
    requests = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500, request=request)

    dispatch(database, receive)
    assert requests == []


def test_location_revocation_prevents_pending_recommendation_delivery(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    outbox.set_location_consent(call_id, False)
    assert outbox.state(call_id, queued[0]) == "cancelled"
    event_types = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        event_types.append(body["event_type"])
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    assert "recommendation" not in event_types


def test_location_reconsent_uses_a_new_generation(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    old = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    withdrawal = outbox.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3])
    new = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert old is not None and new is not None and old[0] != new[0]
    events = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        events.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    recommendations = [event for event in events if event["event_type"] == "recommendation"]
    assert len(recommendations) == 1 and recommendations[0]["location_generation"] == 2


def test_transient_failure_preserves_event_for_retry(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None
    requests = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(503, request=request)

    dispatch(database, unavailable)
    assert outbox.state(call_id, queued[0]) == "pending"

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    assert requests[0] == requests[1]
    assert outbox.state(call_id, queued[0]) == "sent"


def test_invalid_ack_never_marks_recommendation_sent(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    queued = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert queued is not None

    def wrong_ack(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": "different-event", "call_id": call_id}, request=request)

    dispatch(database, wrong_ack)
    assert outbox.state(call_id, queued[0]) != "sent"


def test_legacy_call_consent_does_not_authorize_municipal_delivery(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("UPDATE calls SET consent_version='prototype-v1' WHERE id=?", (call_id,))
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is not None
    requests = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(500, request=request)

    dispatch(database, receive)
    assert requests == []


def test_revocation_after_selection_sends_no_stale_recommendation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.municipal_recommend import MunicipalRecommendationDispatch

    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is not None
    original = MunicipalRecommendationDispatch._next_recommendation

    def revoke_after_selection(self):
        item = original(self)
        if item is not None:
            outbox.set_location_consent(call_id, False)
        return item

    monkeypatch.setattr(MunicipalRecommendationDispatch, "_next_recommendation", revoke_after_selection)
    events = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        events.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    assert [event["event_type"] for event in events] == ["location_revoked"]


def test_failed_revocation_blocks_new_generation_recommendation(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    withdrawal = outbox.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None
    outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3])
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is not None
    events = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        events.append(json.loads(request.content))
        return httpx2.Response(503, request=request)

    dispatch(database, unavailable)
    assert [event["event_type"] for event in events] == ["location_revoked"]


def test_revocation_after_successful_delivery_is_a_durable_event(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True) is not None
    events = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        events.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    outbox.set_location_consent(call_id, False)
    dispatch(database, receive)
    dispatch(database, receive)
    assert [event["event_type"] for event in events] == ["recommendation", "location_revoked"]
    assert events[1]["location_generation"] == events[0]["location_generation"]


def test_failed_revocation_is_reported_as_pending(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    outbox.set_location_consent(call_id, False)
    events = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        events.append(json.loads(request.content))
        return httpx2.Response(503, request=request)

    result = dispatch(database, unavailable)
    assert [event["event_type"] for event in events] == ["location_revoked"]
    assert result.delivered == 0 and result.pending == 1


def test_full_consent_revocation_delivers_location_tombstone(tmp_path: Path) -> None:
    from app.audio_store import AudioStore
    from app.call_revocation import revoke_call

    database, call_id, outbox = setup_call(tmp_path)
    AudioStore(database).initialize()
    outbox.set_location_consent(call_id, True)
    assert outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    events = []

    def receive(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        events.append(body)
        return httpx2.Response(201, json={"applied": True, "duplicate": False,
            "event_id": body["event_id"], "call_id": body["call_id"]}, request=request)

    dispatch(database, receive)
    assert revoke_call(database, call_id)
    dispatch(database, receive)
    assert [event["event_type"] for event in events] == ["recommendation", "location_revoked"]
    assert events[1]["location_generation"] == events[0]["location_generation"]


def test_recommendation_dependent_turn_carries_its_location_generation(tmp_path: Path) -> None:
    from app.municipal_bridge import MunicipalOutbox

    database, call_id, recommendations = setup_call(tmp_path)
    recommendations.set_location_consent(call_id, True)
    recommendation = recommendations.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert recommendation is not None
    outbox = MunicipalOutbox(database)
    outbox.initialize()
    turn, _ = database.create_turn(call_id, "dependent", "그 병원 전화번호는요?")
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO recommendation_turn_contexts(turn_id,event_id,generation) VALUES(?,?,1)",
                    (turn["id"], recommendation[0]))
        con.execute("UPDATE turns SET user_location_generation=1 WHERE id=?", (turn["id"],))
        con.execute("UPDATE turns SET user_location_revision=23 WHERE id=?", (turn["id"],))
    database.update_turn(str(turn["id"]), {"status": "completed", "final_text": "062-000-0000입니다.",
                                         "sources_json": '[]'})
    assert outbox.enqueue_turn(call_id, "dependent")
    with closing(database.connect()) as con:
        body = con.execute("SELECT body FROM municipal_outbox WHERE event_id=?", (f"turn:{turn['id']}",)).fetchone()
    payload = json.loads(body["body"])
    assert payload["location_generations"] == [1]
    assert payload["recommendation_event_ids"] == [recommendation[0]]
    assert payload["utterance_id"] == "dependent"
    assert payload["user_location_generation"] == 1
    assert "user_location_revision" not in payload


def test_explicit_recommendation_event_id_is_idempotent_and_rejects_changes(tmp_path: Path) -> None:
    from app.db import DuplicateRequest

    _, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    first = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True, event_id="rec:stable")
    retry = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True, event_id="rec:stable")
    assert first == retry == ("rec:stable", "pending")
    changed = sample_result().model_copy(update={"warning": "different"})
    with pytest.raises(DuplicateRequest):
        outbox.enqueue(call_id, changed, specialty="내과", location_consent=True, event_id="rec:stable")


def test_patient_location_reset_invalidates_old_generation_once(tmp_path: Path) -> None:
    from app.call_facts import initialize as initialize_facts
    from app.call_utterances import PendingUtterances
    from app.recommendation_navigation import save, snapshot

    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    old = outbox.enqueue(call_id, sample_result(), specialty="내과", location_consent=True)
    assert old is not None
    initialize_facts(database)
    utterances = PendingUtterances(database)
    utterances.initialize()
    assert utterances.admit(call_id, "utterance-1", "어머니 병원을 찾아주세요", "general")
    utterances.classified(call_id, "utterance-1", "recommend", expects_turn=False)
    previous = snapshot(database, call_id, 1)
    assert save(database, call_id, previous, "내과", "내과", "chosen", None, ("test-1",), sample_result())
    with closing(database.connect()) as con:
        source_revision = int(con.execute("SELECT MAX(id) FROM location_consent_events WHERE call_id=?", (call_id,)).fetchone()[0])
    assert outbox.reset_location_generation(call_id, "utterance-1", source_location_generation=0) is None
    assert outbox.location_generation(call_id) == 1
    assert outbox.reset_location_generation(call_id, "utterance-1", source_location_generation=1, source_location_revision=0) is None
    assert outbox.reset_location_generation(call_id, "utterance-1", source_location_generation=1, source_location_revision=source_revision) == 2
    assert outbox.reset_location_generation(call_id, "utterance-1", source_location_generation=1, source_location_revision=source_revision) == 2
    epoch = outbox.reset_location_generation_with_epoch(call_id, "utterance-1", source_location_generation=1, source_location_revision=source_revision)
    assert epoch is not None and epoch[0] == 2 and epoch[1] > source_revision
    assert outbox.location_generation(call_id) == 2
    assert snapshot(database, call_id, 2).chosen_specialty is None
    assert outbox.state(call_id, old[0]) == "cancelled"
    with closing(database.connect()) as con:
        events = con.execute("SELECT generation FROM municipal_location_revocations WHERE call_id=?", (call_id,)).fetchall()
        boundary = con.execute("SELECT utterance_id FROM call_fact_subject_boundaries WHERE call_id=?", (call_id,)).fetchone()
    assert [row["generation"] for row in events] == [1]
    assert boundary["utterance_id"] == "utterance-1"


def test_patient_reset_requires_a_classified_utterance(tmp_path: Path) -> None:
    from app.call_facts import initialize as initialize_facts
    from app.call_utterances import PendingUtterances

    database, call_id, outbox = setup_call(tmp_path)
    outbox.set_location_consent(call_id, True)
    initialize_facts(database)
    PendingUtterances(database).initialize()
    assert outbox.reset_location_generation(call_id, "unknown") is None
    assert outbox.location_generation(call_id) == 1


def test_reconsent_rejects_delayed_previous_generation(tmp_path: Path) -> None:
    _, call_id, outbox = setup_call(tmp_path)
    assert outbox.set_location_consent(call_id, True) is not None
    withdrawal = outbox.set_location_consent_with_epoch(call_id, False)
    assert withdrawal is not None

    assert outbox.set_location_consent(call_id, True, source_location_generation=1) is None
    assert outbox.location_generation(call_id) is None
    assert outbox.set_location_consent(call_id, True, source_location_generation=0, source_location_revision=withdrawal[3]) is not None
    assert outbox.location_generation(call_id) == 2


def test_location_consent_result_carries_transaction_generation(tmp_path: Path) -> None:
    database, call_id, outbox = setup_call(tmp_path)
    assert outbox.set_location_consent_with_generation(call_id, True) == ('accepted', 0, 1)
    assert outbox.set_location_consent_with_generation(call_id, False) == ('revoked', 0, 0)
    with closing(database.connect()) as con:
        revision = int(con.execute('SELECT MAX(id) FROM location_consent_events WHERE call_id=?', (call_id,)).fetchone()[0])
    assert outbox.set_location_consent_with_generation(call_id, True, source_location_generation=0, source_location_revision=revision) == ('accepted', 0, 2)


def test_location_consent_epoch_distinguishes_repeated_revoked_states(tmp_path: Path) -> None:
    _, call_id, outbox = setup_call(tmp_path)
    first = outbox.set_location_consent_with_epoch(call_id, True)
    withdrawn = outbox.set_location_consent_with_epoch(call_id, False)
    assert withdrawn is not None
    reaccepted = outbox.set_location_consent_with_epoch(call_id, True, source_location_generation=0, source_location_revision=withdrawn[3])
    withdrawn_again = outbox.set_location_consent_with_epoch(call_id, False)
    assert first is not None and withdrawn is not None and reaccepted is not None and withdrawn_again is not None
    assert withdrawn[2] == withdrawn_again[2] == 0
    assert first[3] < withdrawn[3] < reaccepted[3] < withdrawn_again[3]
    assert outbox.set_location_consent_with_epoch(call_id, True, source_location_generation=0, source_location_revision=withdrawn[3]) is None
    assert outbox.location_generation(call_id) is None
