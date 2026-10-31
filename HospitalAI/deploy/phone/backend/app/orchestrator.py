"""Concurrent response orchestration for an already-claimed turn."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Protocol

import anyio
import httpx2

from app.audio_store import Audio, AudioStore
from app.call_store import CallStore
from app.call_specialty import select_call_specialty
from app.conversation_context import build_conversation_context
from app.db import Database, Row
from app.response_format import ResponseFormatError, parse_generated_answer
from app.services import (
    EMERGENCY_MESSAGE,
    PESTICIDE_EMERGENCY_MESSAGE,
    INSUFFICIENT_MESSAGE,
    WAIT_MESSAGE,
    WAIT_MESSAGES,
    ServiceError,
    TTSResult,
    emergency_detected,
    pesticide_dizziness,
)


class LanguageModel(Protocol):
    async def low(self, text: str) -> str: ...

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str: ...


class SpeechOutput(Protocol):
    async def deliver(self, text: str, kind: str) -> TTSResult: ...


@dataclass(frozen=True, slots=True)
class ServiceBundle:
    llm: LanguageModel
    tts: SpeechOutput
    low_timeout: float = 2.0
    reasoning_timeout: float = 45.0
    tts_timeout: float = 3.0


def _visit_fallback(error: str, has_specialty: bool) -> str:
    """Provide preparation help without inferring diagnosis or urgency."""
    if error == "grounding_unavailable":
        opening = "상담 자료가 부족해요."
    else:
        opening = "지금은 상담 정보를 확인하기 어려워요."
    question = "가까운 병원을 찾아드릴까요?" if has_specialty else "원하시는 진료과가 있으세요?"
    return f"{opening} 병원에 갈 때 신분증과 복용 중인 약을 챙겨 주세요. {question}"


async def process_turn(db: Database, services: ServiceBundle, session: Row, turn: Row) -> None:
    """Persist early and final responses; the caller schedules only newly claimed turns."""
    turn_id = str(turn["id"])
    text = str(turn["input_text"])
    patient_id = session["patient_id"]
    if patient_id is not None:
        patient_id = str(patient_id)
    started = time.monotonic()
    timings: dict[str, float] = {}
    delivery: dict[str, str] = {}
    sources: list[dict[str, str]] = []
    final_text = INSUFFICIENT_MESSAGE
    speech_text = INSUFFICIENT_MESSAGE
    error: str | None = None
    calls = CallStore(db)
    call_id = str(session["id"])
    is_call = await anyio.to_thread.run_sync(calls.has_call, call_id)

    async def persist(fields: Row) -> None:
        if is_call:
            await anyio.to_thread.run_sync(calls.update_turn_if_safe, turn_id, fields)
        else:
            await anyio.to_thread.run_sync(db.update_turn, turn_id, fields)

    async def urgent_override() -> bool:
        if not is_call:
            return False
        call = await anyio.to_thread.run_sync(calls.get, call_id)
        if call is None or bool(call.get("urgent")) or call.get("state") != "accepted":
            return True
        current = await anyio.to_thread.run_sync(db.get_turn, call_id, str(turn["request_id"]))
        return current is None or current.get("status") == "interrupted"

    async def speak(message: str, kind: str) -> None:
        if await urgent_override():
            return
        delivery[kind] = "dispatching"
        try:
            with anyio.fail_after(services.tts_timeout):
                spoken_message = re.sub(r"\[(?:patient|clinic):[^\]]+\]", "", message).strip() or INSUFFICIENT_MESSAGE
                result = await services.tts.deliver(spoken_message, kind)
                if result.audio is not None and result.mime_type is not None:
                    audio = Audio(result.audio, result.mime_type,
                                  spoken_message if result.status == "synthesized" else None)
                    if is_call:
                        saved = await anyio.to_thread.run_sync(calls.save_audio_if_safe, turn_id, kind, audio)
                        if not saved:
                            delivery[kind] = "cancelled"
                            return
                    else:
                        await anyio.to_thread.run_sync(AudioStore(db).save, turn_id, kind, audio)
                    if result.status == "synthesized":
                        timings[f"{kind}_audio_ready_ms"] = round((time.monotonic() - started) * 1000, 2)
            delivery[kind] = result.status
            await persist({"tts_json": json.dumps(delivery), "timings_json": json.dumps(timings)})
        except (TimeoutError, ServiceError, httpx2.HTTPError, OSError, RuntimeError, sqlite3.Error):
            # A status write can fail after audio was successfully stored; do not
            # misreport synthesis as failed in that case. The caller persists again.
            if delivery[kind] == "dispatching":
                delivery[kind] = "failed"
        finally:
            # Cancellation can leave an external adapter's delivery outcome unknown.
            if delivery[kind] == "dispatching":
                delivery[kind] = "unknown"

    if emergency_detected(text):
        urgent_message = PESTICIDE_EMERGENCY_MESSAGE if pesticide_dizziness(text) else EMERGENCY_MESSAGE
        timings["final_ms"] = round((time.monotonic() - started) * 1000, 2)
        await persist({"status": "emergency", "final_text": urgent_message,
                       "sources_json": "[]", "timings_json": json.dumps(timings),
                       "tts_json": json.dumps(delivery)})
        await speak(urgent_message, "emergency")
        await persist({"tts_json": json.dumps(delivery), "timings_json": json.dumps(timings)})
        return

    final_ready = anyio.Event()
    low_scope = anyio.CancelScope()

    async def initial() -> None:
        with low_scope:
            try:
                with anyio.fail_after(services.low_timeout):
                    message = await services.llm.low(text)
            except (TimeoutError, ServiceError, httpx2.HTTPError, OSError, RuntimeError):
                message = WAIT_MESSAGE
            if message not in WAIT_MESSAGES:
                message = WAIT_MESSAGE
            if final_ready.is_set():
                return
            timings["initial_ms"] = round((time.monotonic() - started) * 1000, 2)
            # Record the generated message before dispatch; delivery is tracked separately.
            await persist({"initial_text": message, "timings_json": json.dumps(timings)})
            if final_ready.is_set():
                return
            await speak(message, "low")
            if final_ready.is_set():
                return
            await persist({"tts_json": json.dumps(delivery), "timings_json": json.dumps(timings)})

    async def final() -> None:
        nonlocal final_text, speech_text, error, sources
        search_started = time.monotonic()
        history: list[Row] = []
        try:
            with anyio.fail_after(services.reasoning_timeout):
                history = await anyio.to_thread.run_sync(db.history, call_id)
                conversation = build_conversation_context(history, turn)
                found = await anyio.to_thread.run_sync(db.search, patient_id, conversation.search_query)
                sources = [{str(key): str(value) for key, value in row.items()} for row in found]
                timings["search_ms"] = round((time.monotonic() - search_started) * 1000, 2)
                if sources:
                    result = parse_generated_answer(await services.llm.reasoning(conversation.prompt_text, sources))
                    final_text, speech_text = result.display, result.speech
                    if final_text == INSUFFICIENT_MESSAGE:
                        error = "grounding_unavailable"
                else:
                    error = "grounding_unavailable"
        except ResponseFormatError:
            error = "invalid_answer_format"
            final_text = INSUFFICIENT_MESSAGE
            speech_text = final_text
        except (TimeoutError, ServiceError, httpx2.HTTPError, OSError, RuntimeError, sqlite3.Error):
            error = "retrieval_or_reasoning_unavailable"
            final_text = "지금은 정보를 확인할 수 없습니다. 의료기관에 문의해 주세요."
            speech_text = final_text
        if is_call and error:
            selection = select_call_specialty(None, history)
            final_text = _visit_fallback(error, selection.specialty is not None)
            speech_text = final_text
        final_ready.set()
        low_scope.cancel()
        timings["final_ms"] = round((time.monotonic() - started) * 1000, 2)
        await persist({"final_text": final_text,
                       "sources_json": json.dumps(sources, ensure_ascii=False),
                       "timings_json": json.dumps(timings)})
        await speak(speech_text, "final")

    await persist({"status": "processing"})
    if await urgent_override():
        return
    finished = anyio.Event()

    async def final_and_signal() -> None:
        try:
            await final()
        finally:
            finished.set()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(initial)
        tasks.start_soon(final_and_signal)
        if is_call:
            async def watch_urgent() -> None:
                while not finished.is_set():
                    if await urgent_override():
                        tasks.cancel_scope.cancel()
                        return
                    with anyio.move_on_after(0.15):
                        await finished.wait()
            tasks.start_soon(watch_urgent)

    if await urgent_override():
        return

    await persist({"status": "degraded" if error else "completed", "final_text": final_text,
                   "sources_json": json.dumps(sources, ensure_ascii=False),
                   "timings_json": json.dumps(timings), "tts_json": json.dumps(delivery),
                   "error": error})

