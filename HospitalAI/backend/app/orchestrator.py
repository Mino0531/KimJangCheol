"""Concurrent response orchestration for an already-claimed turn."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Protocol

import anyio
import httpx2

from app.audio_store import Audio, AudioStore
from app.call_speed import call_speech_speed, deliver_at_speed
from app.call_store import CallStore
from app.call_specialty import select_call_specialty
from app.conversation_context import build_conversation_context, redact_superseded
from app.conversation_admission import ConversationScope, admit_conversation
from app.call_facts import location_generation
from app.location_private_mode import private_fallback_for_turn
from app.utterance_location import validate_source_generation
from app.conversation_responses import SYMPTOM_OFFER, is_grounded_service_response
from app.fall_guidance import Guidance, asks_patient_specialty, guided_response, last_guidance, medical_question, walking_answer
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


def _visit_fallback(error: str, specialty: str | None, already_offered: bool) -> str:
    """Provide preparation help without inferring diagnosis or urgency."""
    if already_offered:
        return "자세한 상담 답변은 지금 확인하기 어려워요. 병원 찾기나 방문 준비 중 어떤 걸 더 도와드릴까요?"
    if specialty:
        return "증상은 의료기관에서 확인받으시는 게 좋겠어요. 가까운 종합병원을 찾아드릴까요?"
    return "정확한 상태는 의료기관에서 확인받으시는 게 좋겠어요. 가까운 의료기관을 찾아드릴까요?"


def _source_generation_current(db: Database, call_id: str, generation: int | None, revision: int | None) -> bool:
    with closing(db.connect()) as con:
        return validate_source_generation(con, call_id, generation, revision)


def _private_turn_count(db: Database, call_id: str) -> int:
    with closing(db.connect()) as con:
        return int(con.execute("SELECT count(*) FROM turns WHERE session_id=? AND error='location_private_fallback' AND status='degraded'",
                               (call_id,)).fetchone()[0])


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
        if call is None or bool(call.get("urgent")) or call.get("state") != "accepted" or call.get("ended_at"):
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
                speed = await anyio.to_thread.run_sync(call_speech_speed, db, call_id) if is_call else 1.0
                result = await deliver_at_speed(services.tts, spoken_message, kind, speed)
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

    source_generation = turn.get('user_location_generation')
    if not isinstance(source_generation, int):
        source_generation = None
    source_revision = turn.get('user_location_revision')
    if not isinstance(source_revision, int):
        source_revision = None
    if is_call and not await anyio.to_thread.run_sync(_source_generation_current, db, call_id, source_generation, source_revision):
        await persist({'status': 'interrupted', 'final_text': None, 'sources_json': None,
                       'error': 'location_context_revoked'})
        return

    if emergency_detected(text):
        urgent_message = PESTICIDE_EMERGENCY_MESSAGE if pesticide_dizziness(text) else EMERGENCY_MESSAGE
        timings["final_ms"] = round((time.monotonic() - started) * 1000, 2)
        await persist({"status": "emergency", "final_text": urgent_message,
                       "sources_json": "[]", "timings_json": json.dumps(timings),
                       "tts_json": json.dumps(delivery)})
        await speak(urgent_message, "emergency")
        await persist({"tts_json": json.dumps(delivery), "timings_json": json.dumps(timings)})
        return

    if is_call and await anyio.to_thread.run_sync(location_generation, db, call_id) is None:
        # No caller-supplied text is sent to the LLM, retrieval, or TTS in this mode.
        count = await anyio.to_thread.run_sync(_private_turn_count, db, call_id)
        guidance = private_fallback_for_turn(count)
        await persist({'status': 'processing', 'final_text': guidance, 'sources_json': '[]',
                       'error': 'location_private_fallback', 'tts_json': json.dumps(delivery)})
        await speak(guidance, 'final')
        await persist({'status': 'degraded', 'tts_json': json.dumps(delivery)})
        return

    final_ready = anyio.Event()
    low_scope = anyio.CancelScope()

    async def initial() -> None:
        with low_scope:
            try:
                with anyio.fail_after(services.low_timeout):
                    # The waiting-message selector only needs an allowlisted
                    # phrase, not the caller's raw utterance.
                    message = await services.llm.low('' if is_call else text)
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
        current_turn = turn
        guidance: Guidance | None = None
        try:
            with anyio.fail_after(services.reasoning_timeout):
                history = await anyio.to_thread.run_sync(db.history, call_id)
                recommendation = None
                if is_call:
                    admitted = await anyio.to_thread.run_sync(admit_conversation, db,
                        ConversationScope(call_id, turn_id, str(turn['request_id']),
                                          source_location_generation=source_generation,
                                          source_location_revision=source_revision,
                                          enforce_source_generation=True), history)
                    history, recommendation = admitted.history, admitted.recommendation
                    if not admitted.active:
                        final_ready.set()
                        low_scope.cancel()
                        await persist({'status': 'interrupted', 'final_text': None,
                                       'sources_json': None, 'error': 'location_context_revoked'})
                        return
                    current = next((row for row in history if row.get('id') == turn_id), None)
                    if current is None or not str(current.get('input_text') or '').strip():
                        final_ready.set()
                        low_scope.cancel()
                        await persist({'status': 'interrupted', 'final_text': None,
                                       'sources_json': None, 'error': 'location_context_revoked'})
                        return
                    current_turn = current
                facts = ()
                search_patient_id = None if is_call and admitted.patient_subject_changed else patient_id
                if is_call and admitted.facts is not None:
                    snapshot = admitted.facts
                    facts = snapshot.current
                    if snapshot.patient_subject_changed:
                        search_patient_id = None
                    history = redact_superseded(history, snapshot.superseded, snapshot.subject_excluded_utterance_ids)
                conversation = build_conversation_context(history, current_turn, recommendation, facts=facts)
                prior_history = [row for row in history if row.get('id') != turn_id]
                guided = guided_response(str(current_turn['input_text']), prior_history) if is_call else None
                prior_guidance = last_guidance(prior_history) if is_call else None
                dependent_answer = (prior_guidance is not None and prior_guidance.action == 'follow_up'
                                    and prior_guidance.question_id == 'fall_walking'
                                    and walking_answer(str(current_turn['input_text'])) in ('yes', 'unknown'))
                if dependent_answer:
                    parent_id = str(prior_history[-1]['id'])
                    if not await anyio.to_thread.run_sync(calls.bind_guidance_parent, turn_id, parent_id):
                        final_ready.set()
                        low_scope.cancel()
                        return
                if guided is not None:
                    final_text = speech_text = guided.text
                    guidance = guided.guidance
                else:
                    found = await anyio.to_thread.run_sync(db.search, search_patient_id, conversation.search_query)
                    sources = [{str(key): str(value) for key, value in row.items()} for row in found]
                    if recommendation is not None:
                        sources.append(dict(recommendation.as_source()))
                    timings["search_ms"] = round((time.monotonic() - search_started) * 1000, 2)
                    if sources or is_call:
                        if await urgent_override():
                            final_ready.set()
                            low_scope.cancel()
                            return
                        result = parse_generated_answer(await services.llm.reasoning(conversation.prompt_text, sources))
                        final_text, speech_text = result.display, result.speech
                        service_only = is_grounded_service_response(result, conversation.prompt_text)
                        if is_call and (asks_patient_specialty(final_text) or asks_patient_specialty(speech_text)):
                            error = 'unsafe_department_question'
                        elif (final_text == INSUFFICIENT_MESSAGE or
                                (result.display == SYMPTOM_OFFER and not service_only) or
                                (not sources and not service_only)):
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
            earlier_fallback = any(row.get("id") != turn_id and row.get("status") == "degraded" for row in history)
            final_text = _visit_fallback(error, selection.specialty, earlier_fallback)
            if medical_question(str(current_turn.get('input_text') or '')):
                final_text = "질문하신 내용은 지금 정확히 확인하기 어려워요. 의료진이나 약사에게 확인해 주세요."
                guidance = Guidance(action='answer')
            elif turn.get("caller_context") == "visit_preparation_offer":
                final_text = "신분증과 드시는 약을 챙겨 주세요. 더 궁금한 준비물이 있으세요?"
            elif not earlier_fallback:
                guidance = Guidance(action='hospital_search_offer', specialty=selection.specialty)
            speech_text = final_text
        final_ready.set()
        low_scope.cancel()
        timings["final_ms"] = round((time.monotonic() - started) * 1000, 2)
        await persist({"final_text": final_text,
                       "sources_json": json.dumps(sources, ensure_ascii=False),
                       "guidance_json": guidance.model_dump_json() if guidance is not None else None,
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

