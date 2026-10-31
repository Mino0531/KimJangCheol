"""Urgent override cancels unfinished routine output without fabricating symptoms."""

from pathlib import Path
from contextlib import closing

import anyio

from app.audio_store import AudioStore
from app.call_store import CallStore
from app.db import Database
from app.orchestrator import ServiceBundle, process_turn
from app.services import MockTTS


def test_urgent_override_persists_without_creating_a_symptom(tmp_path: Path) -> None:
    db = Database(tmp_path / "urgent.sqlite")
    db.initialize()
    calls = CallStore(db)
    calls.initialize()
    session = db.create_session("+821000000001")
    calls.create(str(session["id"]))
    assert calls.mark_urgent(str(session["id"]))
    call = calls.get(str(session["id"]))
    assert call is not None and call["urgent"] == 1
    assert db.history(str(session["id"])) == []


def test_urgent_override_cancels_inflight_routine_response(tmp_path: Path) -> None:
    db = Database(tmp_path / "urgent.sqlite")
    db.initialize(seed=True)
    calls = CallStore(db)
    calls.initialize()
    AudioStore(db).initialize()
    session = db.create_session("+821000000001")
    calls.create(str(session["id"]))
    assert calls.decide(str(session["id"]), "accepted")
    turn, _ = db.create_turn(str(session["id"]), "1", "기침이 있어요")

    async def scenario() -> None:
        low_started = anyio.Event()
        high_started = anyio.Event()
        finished = anyio.Event()

        class SlowModel:
            async def low(self, text: str) -> str:
                low_started.set()
                await anyio.sleep_forever()
                return ""

            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                high_started.set()
                await anyio.sleep_forever()
                return ""

        async def process() -> None:
            await process_turn(db, ServiceBundle(SlowModel(), MockTTS()), session, turn)
            finished.set()

        async with anyio.create_task_group() as group:
            group.start_soon(process)
            with anyio.fail_after(3):
                await low_started.wait()
                await high_started.wait()
                assert await anyio.to_thread.run_sync(calls.mark_urgent, str(session["id"]))
                await finished.wait()

    anyio.run(scenario)
    stored = db.get_turn(str(session["id"]), "1")
    assert stored is not None
    assert stored["status"] == "cancelled"
    assert stored["final_text"] is None
    assert AudioStore(db).get(str(turn["id"]), "final") is None


def test_existing_calls_table_is_migrated_without_losing_consent(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy.sqlite")
    db.initialize()
    session = db.create_session("+821000000001")
    with closing(db.connect()) as con, con:
        con.execute("CREATE TABLE calls (id TEXT PRIMARY KEY REFERENCES sessions(id), state TEXT NOT NULL)")
        con.execute("INSERT INTO calls(id, state) VALUES (?, 'accepted')", (session["id"],))
    calls = CallStore(db)
    calls.initialize()
    stored = calls.get(str(session["id"]))
    assert stored is not None and stored["state"] == "accepted" and stored["urgent"] == 0


def test_urgent_blocks_late_audio_and_text_writes(tmp_path: Path) -> None:
    db = Database(tmp_path / "urgent.sqlite")
    db.initialize()
    AudioStore(db).initialize()
    calls = CallStore(db)
    calls.initialize()
    session = db.create_session("+821000000001")
    calls.create(str(session["id"]))
    turn, _ = db.create_turn(str(session["id"]), "1", "test")
    assert calls.mark_urgent(str(session["id"]))
    assert not calls.update_turn_if_safe(str(turn["id"]), {"final_text": "late response"})
    from app.audio_store import Audio
    assert not calls.save_audio_if_safe(str(turn["id"]), "final", Audio(b"audio", "audio/mpeg"))
    assert AudioStore(db).get(str(turn["id"]), "final") is None
