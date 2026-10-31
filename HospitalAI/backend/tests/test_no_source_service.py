"""Source-free call replies are limited to fixed service conversation."""

import json
from pathlib import Path

import pytest

from app.audio_store import AudioStore
from app.call_store import CallStore
from app.conversation_responses import SERVICE_RESPONSES
from app.db import Database
from app.orchestrator import ServiceBundle, process_turn
from app.services import MockTTS, WAIT_MESSAGE


class FixedAnswer:
    """Return a supplied model answer without making external calls."""

    def __init__(self, display: str, speech: str, citations: list[str]) -> None:
        self.answer = json.dumps({"display_text": display, "tts_text": speech, "citations": citations})

    async def low(self, text: str) -> str:
        return WAIT_MESSAGE

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        assert sources == []
        return self.answer


async def run_answer(tmp_path: Path, answer: FixedAnswer) -> dict[str, str | int | float | None]:
    database = Database(tmp_path / "source-free.sqlite")
    database.initialize()
    AudioStore(database).initialize()
    calls = CallStore(database)
    calls.initialize()
    session = database.create_session("+821000000001")
    call_id = str(session["id"])
    calls.create(call_id)
    assert calls.decide(call_id, "accepted")
    turn, _ = database.create_turn(call_id, "social", "고마워요")
    await process_turn(database, ServiceBundle(answer, MockTTS()), session, turn)
    saved = database.get_turn(call_id, "social")
    assert saved is not None
    return saved


@pytest.mark.anyio
@pytest.mark.parametrize("response", list(SERVICE_RESPONSES.values()))
async def test_source_free_exact_service_reply_keeps_call_answer_completed(tmp_path: Path, response: str) -> None:
    saved = await run_answer(tmp_path, FixedAnswer(response, response, []))
    assert saved["status"] == "completed" and saved["error"] is None
    assert saved["final_text"] == response


@pytest.mark.anyio
@pytest.mark.parametrize("display,speech,citations", [
    ("이 약을 드세요", "이 약을 드세요", []),
    (SERVICE_RESPONSES["hold"], SERVICE_RESPONSES["hold"], ["clinic:forged"]),
    (SERVICE_RESPONSES["hold"], "이 약을 드세요", []),
])
async def test_source_free_clinical_or_forged_reply_still_uses_safe_fallback(
    tmp_path: Path, display: str, speech: str, citations: list[str],
) -> None:
    saved = await run_answer(tmp_path, FixedAnswer(display, speech, citations))
    assert saved["status"] == "degraded" and saved["error"] == "grounding_unavailable"
    assert "이 약" not in str(saved["final_text"])
