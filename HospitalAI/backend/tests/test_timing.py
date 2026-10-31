"""Audio readiness metrics describe synthesized audio rather than playback."""

import json
import sqlite3
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from app.audio_store import AudioStore
from app.config import Settings
from app.db import Database, Row
from app.main import create_app
from app.orchestrator import ServiceBundle, process_turn
from app.services import MockLLM, MockTTS, ServiceError, TTSResult


class SyntheticTTS:
    async def deliver(self, text: str, kind: str) -> TTSResult:
        return TTSResult(status="synthesized", kind=kind, text=text, interruptible=False,
                         audio=b"audio-" + kind.encode(), mime_type="audio/mpeg")


def test_final_audio_ready_time_is_returned_after_audio_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "timing.sqlite"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), SyntheticTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        session = client.post("/sessions", json={"phone": "+821000000001", "verification_token": "verify"}).json()["id"]
        client.post(f"/sessions/{session}/turns", json={"request_id": "r1", "text": "기침이 있어요"})
        turn = client.get(f"/sessions/{session}/turns/r1").json()
        response = client.get(f"/sessions/{session}/turns/r1/audio/final")
    timings = json.loads(turn["timings_json"])
    assert response.content == b"audio-final"
    assert timings["final_audio_ready_ms"] >= timings["final_ms"]


def test_no_audio_ready_time_is_reported_for_simulated_tts(tmp_path: Path) -> None:
    path = tmp_path / "mock.sqlite"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        session = client.post("/sessions", json={"phone": "+821000000001", "verification_token": "verify"}).json()["id"]
        client.post(f"/sessions/{session}/turns", json={"request_id": "r1", "text": "기침이 있어요"})
        turn = client.get(f"/sessions/{session}/turns/r1").json()
    timings = json.loads(turn["timings_json"])
    assert "final_audio_ready_ms" not in timings


def test_synthesis_failure_never_claims_audio_is_ready(tmp_path: Path) -> None:
    class BrokenTTS:
        async def deliver(self, text: str, kind: str) -> TTSResult:
            raise ServiceError("synthetic provider failure")

    path = tmp_path / "failed.sqlite"
    settings = Settings(environment="development", service_token="service", admin_token="admin",
                        dev_verification_token="verify", seed_demo=True, database_path=str(path))
    app = create_app(settings, Database(path), ServiceBundle(MockLLM(), BrokenTTS()))
    with TestClient(app, headers={"Authorization": "Bearer service"}) as client:
        session = client.post("/sessions", json={"phone": "+821000000001", "verification_token": "verify"}).json()["id"]
        client.post(f"/sessions/{session}/turns", json={"request_id": "r1", "text": "기침이 있어요"})
        turn = client.get(f"/sessions/{session}/turns/r1").json()
    assert json.loads(turn["tts_json"])["final"] == "failed"
    assert "final_audio_ready_ms" not in json.loads(turn["timings_json"])


def test_stored_audio_is_not_marked_failed_when_status_update_fails_once(tmp_path: Path) -> None:
    class IntermittentDatabase(Database):
        failed = False

        def update_turn(self, turn_id: str, fields: Row) -> None:
            if not self.failed and '"emergency": "synthesized"' in str(fields.get("tts_json")):
                self.failed = True
                raise sqlite3.OperationalError("temporary lock")
            super().update_turn(turn_id, fields)

    db = IntermittentDatabase(tmp_path / "intermittent.sqlite")
    db.initialize(seed=True)
    AudioStore(db).initialize()
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "r1", "숨을 쉴 수 없어요")

    anyio.run(process_turn, db, ServiceBundle(MockLLM(), SyntheticTTS()), session, turn)

    stored = db.get_turn(str(session["id"]), "r1")
    assert stored is not None
    assert AudioStore(db).get(str(turn["id"]), "emergency") is not None
    assert json.loads(str(stored["tts_json"]))["emergency"] == "synthesized"


def test_low_audio_ready_time_is_persisted_before_reasoning_finishes(tmp_path: Path) -> None:
    class NotifyingDatabase(Database):
        low_ready: anyio.Event | None = None

        def update_turn(self, turn_id: str, fields: Row) -> None:
            super().update_turn(turn_id, fields)
            timing_json = fields.get("timings_json")
            if self.low_ready is not None and isinstance(timing_json, str):
                if "low_audio_ready_ms" in json.loads(timing_json):
                    anyio.from_thread.run_sync(self.low_ready.set)

    db = NotifyingDatabase(tmp_path / "low.sqlite")
    db.initialize(seed=True)
    AudioStore(db).initialize()
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "r1", "기침이 있어요")

    async def scenario() -> None:
        allow_final = anyio.Event()
        low_ready = anyio.Event()
        db.low_ready = low_ready

        class GatedLLM(MockLLM):
            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                await allow_final.wait()
                return await super().reasoning(text, sources)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(process_turn, db, ServiceBundle(GatedLLM(), SyntheticTTS()), session, turn)
            with anyio.fail_after(5):
                await low_ready.wait()
            current = await anyio.to_thread.run_sync(db.get_turn, str(session["id"]), "r1")
            assert current is not None
            timing = json.loads(str(current["timings_json"]))
            assert timing["low_audio_ready_ms"] >= timing["initial_ms"]
            assert AudioStore(db).get(str(turn["id"]), "low") is not None
            assert current["status"] == "processing"
            allow_final.set()

    anyio.run(scenario)
