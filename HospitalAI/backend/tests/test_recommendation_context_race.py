"""Location withdrawal must suppress answers derived from an older hospital query."""

import json
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest

from app.audio_store import AudioStore
from app.call_store import CallStore
from app.db import Database
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle, process_turn
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import TTSResult, WAIT_MESSAGE


class RevocationServices:
    """Mutable recording double that changes consent at a precise async boundary."""

    def __init__(self, database: Database, call_id: str, boundary: str, reaccept: bool) -> None:
        self.database = database
        self.call_id = call_id
        self.boundary = boundary
        self.reaccept = reaccept
        self.speech: list[tuple[str, str]] = []
        self.reasoning_calls = 0

    def revoke(self) -> None:
        outbox = RecommendationOutbox(self.database)
        outbox.set_location_consent(self.call_id, False)
        if self.reaccept:
            outbox.set_location_consent(self.call_id, True)

    async def low(self, text: str) -> str:
        return WAIT_MESSAGE

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        self.reasoning_calls += 1
        assert any(source["id"] == "clinic:recent-recommendation" for source in sources)
        if self.boundary == "reasoning":
            self.revoke()
        answer = "가상 병원 번호는 062-111-2222입니다."
        return json.dumps({"display_text": answer, "tts_text": answer,
                           "citations": ["clinic:recent-recommendation"]}, ensure_ascii=False)

    async def deliver(self, text: str, kind: str) -> TTSResult:
        self.speech.append((kind, text))
        if self.boundary == "tts" and kind == "final":
            self.revoke()
        return TTSResult(kind=kind, text=text, interruptible=True, status="synthesized",
                         audio=b"synthetic-audio", mime_type="audio/mpeg")


def prepared(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "context-race.sqlite")
    database.initialize()
    AudioStore(database).initialize()
    calls = CallStore(database)
    calls.initialize()
    MunicipalOutbox(database).initialize()
    session = database.create_session("+821000000001")
    call_id = str(session["id"])
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    hospital = HospitalCandidate(id="public", name="가상 병원", address="공개 주소",
        phone="062-111-2222", latitude=35.0, longitude=126.0, specialties=("내과",),
        specialists={"내과": 1}, source="테스트 가상 공공 자료")
    result = recommend(RecommendationQuery(latitude=35.0, longitude=126.0, specialty="내과"), [hospital])
    assert RecommendationOutbox(database).enqueue(call_id, result, specialty="내과", location_consent=True)
    return database, call_id


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["reasoning", "tts"])
@pytest.mark.parametrize("reaccept", [False, True])
async def test_withdrawal_during_answer_prevents_stale_text_audio_and_municipal_send(
    tmp_path: Path, boundary: Literal["reasoning", "tts"], reaccept: bool,
) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "followup", "그 병원 번호 다시 알려줘")
    services = RevocationServices(database, call_id, boundary, reaccept)
    session = database.get_session(call_id)
    assert session is not None

    await process_turn(database, ServiceBundle(services, services), session, turn)

    saved = database.get_turn(call_id, "followup")
    assert saved is not None
    assert saved["input_text"] == "그 병원 번호 다시 알려줘"
    assert saved["status"] == "interrupted"
    assert saved["error"] == "location_context_revoked"
    assert saved["final_text"] is None and saved["sources_json"] is None
    assert AudioStore(database).get(str(turn["id"]), "final") is None
    assert MunicipalOutbox(database).enqueue_turn(call_id, "followup") is False
    assert CallStore(database).get(call_id)["ended_at"] is None
    if boundary == "reasoning":
        assert all(kind != "final" for kind, _ in services.speech)


@pytest.mark.anyio
async def test_withdrawal_cancels_queued_followup_without_losing_caller_words(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "followup", "그 병원 번호 다시 알려줘")
    services = RevocationServices(database, call_id, "none", False)
    session = database.get_session(call_id)
    assert session is not None
    await process_turn(database, ServiceBundle(services, services), session, turn)
    municipal = MunicipalOutbox(database)
    assert municipal.enqueue_turn(call_id, "followup")

    RecommendationOutbox(database).set_location_consent(call_id, False)

    assert municipal._next() is None
    with closing(database.connect()) as con:
        replacement = con.execute('''SELECT o.body FROM municipal_redacted_events r
            JOIN municipal_outbox o ON o.event_id=r.replacement_event_id WHERE r.original_event_id=?''',
            (f"turn:{turn['id']}",)).fetchone()
    assert replacement is not None
    assert json.loads(replacement['body'])['event_type'] == 'privacy_redacted'
    assert '그 병원 번호 다시 알려줘' not in replacement['body']
    assert municipal._eligible(f"turn:{turn['id']}", call_id) is False
    with closing(database.connect()) as con:
        row = con.execute("SELECT status,input_text,final_text FROM turns WHERE id=?", (turn["id"],)).fetchone()
        payload = json.loads(con.execute("SELECT body FROM municipal_outbox WHERE event_id=?",
                                        (f"turn:{turn['id']}",)).fetchone()[0])
    assert row["status"] == "interrupted" and row["final_text"] is None
    assert row["input_text"] == "그 병원 번호 다시 알려줘"
    assert payload["assistant_text"] == "" and payload["sources"] == []


@pytest.mark.anyio
async def test_dispatch_rechecks_cancelled_snapshot_after_answer_was_queued(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "followup", "그 병원 번호 다시 알려줘")
    services = RevocationServices(database, call_id, "none", False)
    session = database.get_session(call_id)
    assert session is not None
    await process_turn(database, ServiceBundle(services, services), session, turn)
    municipal = MunicipalOutbox(database)
    assert municipal.enqueue_turn(call_id, "followup")
    with closing(database.connect()) as con, con:
        con.execute("""INSERT INTO recommendation_cancellations(event_id)
            SELECT event_id FROM recommendation_turn_contexts WHERE turn_id=?""", (turn["id"],))

    assert municipal._eligible(f"turn:{turn['id']}", call_id) is False
    assert AudioStore(database).get(str(turn["id"]), "final") is None
    assert municipal._next() is None


def test_upgrade_discards_unbound_legacy_hospital_answer_without_fabricating_generation(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "old-build", "병원 전화번호요")
    database.update_turn(str(turn["id"]), {"status": "completed", "final_text": "가상 병원 전화입니다.",
        "sources_json": '[{"id":"clinic:recent-recommendation","type":"clinic"}]'})

    RecommendationOutbox(database).initialize()

    saved = database.get_turn(call_id, "old-build")
    assert saved is not None and saved["status"] == "interrupted"
    assert saved["input_text"] == "병원 전화번호요" and saved["final_text"] is None


@pytest.mark.anyio
async def test_withdrawal_during_search_does_not_start_stale_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "followup", "그 병원 번호 다시 알려줘")
    services = RevocationServices(database, call_id, "none", False)
    session = database.get_session(call_id)
    assert session is not None

    def search_after_withdrawal(patient_id: str | None, query: str) -> list[dict[str, str]]:
        services.revoke()
        return []

    monkeypatch.setattr(database, "search", search_after_withdrawal)
    await process_turn(database, ServiceBundle(services, services), session, turn)

    assert services.reasoning_calls == 0
    saved = database.get_turn(call_id, "followup")
    assert saved is not None and saved["status"] == "interrupted"
