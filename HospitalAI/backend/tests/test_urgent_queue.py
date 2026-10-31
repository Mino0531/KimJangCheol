"""Urgent calls must not later dispatch routine consultation or hospital rankings."""

from contextlib import closing
from pathlib import Path

import anyio
import httpx2

from app.call_store import CallStore
from app.db import Database
from app.hospital_bridge import HospitalConfig, HospitalOutbox
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend


def make_call(tmp_path: Path) -> tuple[Database, str]:
    db = Database(tmp_path / "urgent.sqlite3")
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    with closing(db.connect()) as con, con:
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(calls)")}
        if "urgent" not in columns:
            con.execute("ALTER TABLE calls ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
    call_id = str(db.create_session("+821000000001")["id"])
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    return db, call_id


def mark_urgent(db: Database, call_id: str) -> None:
    with closing(db.connect()) as con, con:
        con.execute("UPDATE calls SET urgent=1 WHERE id=?", (call_id,))


def test_urgent_button_blocks_routine_recommendation_queue_and_delivery(tmp_path: Path) -> None:
    db, call_id = make_call(tmp_path)
    outbox = RecommendationOutbox(db)
    outbox.initialize()
    outbox.set_location_consent(call_id, True)
    result = recommend(RecommendationQuery(latitude=35, longitude=126), [
        HospitalCandidate(id="public1", name="가상 병원", latitude=35, longitude=126, source="가상 자료")])
    queued = outbox.enqueue(call_id, result, specialty=None, location_consent=True)
    assert queued is not None
    mark_urgent(db, call_id)
    assert outbox.enqueue(call_id, result, specialty=None, location_consent=True) is None
    received: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        received.append(request.url.path)
        return httpx2.Response(500, request=request)

    async def run() -> None:
        await outbox.dispatch(HospitalConfig("http://127.0.0.1:8767", "token"),
                              transport=httpx2.MockTransport(handler))

    anyio.run(run)
    assert received == []


def test_urgent_button_blocks_queued_routine_answer_but_preserves_emergency(tmp_path: Path) -> None:
    db, call_id = make_call(tmp_path)
    outbox = HospitalOutbox(db)
    outbox.initialize()
    ordinary, _ = db.create_turn(call_id, "normal", "ordinary")
    db.update_turn(str(ordinary["id"]), {"status": "completed", "final_text": "normal answer"})
    assert outbox.enqueue_turn(call_id, "normal")
    mark_urgent(db, call_id)
    assert outbox._next() is None
    assert not outbox._eligible("turn:" + str(ordinary["id"]), call_id)
    later, _ = db.create_turn(call_id, "later", "ordinary")
    db.update_turn(str(later["id"]), {"status": "completed", "final_text": "late answer"})
    assert not outbox.enqueue_turn(call_id, "later")
    emergency, _ = db.create_turn(call_id, "urgent", "emergency")
    db.update_turn(str(emergency["id"]), {"status": "emergency", "final_text": "urgent answer"})
    assert outbox.enqueue_turn(call_id, "urgent")
    next_item = outbox._next()
    assert next_item is not None
    assert next_item[0] == "turn:" + str(emergency["id"])
