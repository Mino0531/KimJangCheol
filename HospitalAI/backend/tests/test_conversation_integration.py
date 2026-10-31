from pathlib import Path

import anyio

from app.db import Database
from app.orchestrator import ServiceBundle, process_turn
from app.services import INSUFFICIENT_MESSAGE, MockTTS, WAIT_MESSAGE


def test_followup_retrieves_original_symptom_and_continues_same_session(tmp_path: Path) -> None:
    class RecordingLLM:
        def __init__(self) -> None:
            self.requests: list[tuple[str, list[dict[str, str]]]] = []

        async def low(self, text: str) -> str:
            return WAIT_MESSAGE

        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            self.requests.append((text, sources))
            if len(self.requests) == 1:
                return "기침은 언제부터 나셨어요?"
            return "어제부터 기침이 나셨군요. 열도 나세요?"

    db = Database(tmp_path / "conversation.sqlite3")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    other_session = db.create_session("+821000000002", "sample-patient-2")
    other, _ = db.create_turn(str(other_session["id"]), "other", "다른 사람의 개인 발화")
    db.update_turn(str(other["id"]), {"status": "completed", "final_text": "다른 사람의 응답"})
    first, _ = db.create_turn(str(session["id"]), "first", "기침이 나요")
    provider = RecordingLLM()
    services = ServiceBundle(provider, MockTTS())
    anyio.run(process_turn, db, services, session, first)
    second, _ = db.create_turn(str(session["id"]), "second", "어제부터요")

    anyio.run(process_turn, db, services, session, second)

    assert len(provider.requests) == 2
    prompt, sources = provider.requests[1]
    assert "기침이 나요" in prompt
    assert "기침은 언제부터 나셨어요?" in prompt
    assert "어제부터요" in prompt
    assert "다른 사람의 개인 발화" not in prompt
    assert any(source["id"] == "clinic:sample-clinic-1" for source in sources)
    saved = db.get_turn(str(session["id"]), "second")
    assert saved is not None
    assert saved["status"] == "completed"


def test_grounding_fallback_is_recorded_as_degraded(tmp_path: Path) -> None:
    class UngroundedLLM:
        async def low(self, text: str) -> str:
            return WAIT_MESSAGE

        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            assert sources
            return INSUFFICIENT_MESSAGE

    db = Database(tmp_path / "fallback.sqlite3")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "first", "기침이 나요")
    anyio.run(process_turn, db, ServiceBundle(UngroundedLLM(), MockTTS()), session, turn)
    saved = db.get_turn(str(session["id"]), "first")
    assert saved is not None
    assert saved["status"] == "degraded"
    assert saved["error"] == "grounding_unavailable"
    assert saved["final_text"] == INSUFFICIENT_MESSAGE
