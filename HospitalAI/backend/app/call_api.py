"""Consent-gated call simulation and STT text interface."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from functools import partial
from typing import Literal

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import Response

from app.call_consent import ConsentInput, UrgentInput, split_consent
from app.call_context import CallContext
from app.conversation_admission import ConversationScope, admit_conversation
from app.call_prompts import PromptKind, call_payload, render_prompt
from app.call_store import CallStore
from app.call_utterances import PendingUtterances
from app.call_facts import InvalidFact, apply_explicit, initialize as initialize_facts, location_generation
from app.conversation_context import redact_superseded
from app.call_interpret import IntentInput, classify_decision, classify_private_decision, explicit_same_place, explicit_specialty
from app.call_interrupt import interrupt_router
from app.call_playback import playback_router
from app.call_urgent_followup import urgent_followup_router
from app.call_revocation import revoke_call
from app.call_recommendation_api import recommendation_router
from app.call_speed import call_speech_speed
from app.region_resolution import region_router
from app.db import DuplicateRequest, Row, Value
from app.fall_guidance import parse_guidance
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import process_turn
from app.recommend_dashboard import RecommendationOutbox
from app.recommend_api import add_audio, public_recommendations
from app.safe_demo_model import SafeDemoModel
from app.schemas import SessionInput, TurnInput
from app.services import MockLLM, SplitLLM, emergency_detected
from app.stored_response import stored_audio_safe, visible_turn
from app.utterance_location import StaleLocationGeneration

def call_router(context: CallContext) -> APIRouter:
    router = APIRouter(dependencies=[Depends(context.auth)])
    calls = CallStore(context.database)
    utterances = PendingUtterances(context.database)
    utterances.initialize()
    initialize_facts(context.database)
    municipal = MunicipalOutbox(context.database)
    recommendation_outbox = RecommendationOutbox(context.database)
    recommendation_outbox.initialize()
    is_mock = isinstance(context.services.llm, MockLLM) or (
        isinstance(context.services.llm, SplitLLM) and isinstance(context.services.llm.high_provider, MockLLM))
    services = context.services
    if is_mock:
        services = replace(services, llm=SafeDemoModel(context.services.llm))

    def get_call(call_id: str) -> Row:
        context.session_or_404(call_id)
        call = calls.get(call_id)
        if call is None:
            raise HTTPException(404, "Call not found")
        return call

    def require_accepted(call_id: str) -> None:
        if get_call(call_id)["state"] != "accepted":
            raise HTTPException(403, "Consent is required")

    def require_active(call_id: str) -> None:
        require_accepted(call_id)
        if get_call(call_id).get("ended_at"):
            raise HTTPException(409, "Call has ended")

    async def queue_urgent_notice(call_id: str) -> None:
        try:
            await anyio.to_thread.run_sync(municipal.enqueue_urgent, call_id)
            await anyio.to_thread.run_sync(municipal.enqueue_urgent_details, call_id)
        except sqlite3.Error:
            return  # Startup recovery can recreate the event from the urgent call state.
        if context.municipal is not None:
            await municipal.dispatch(context.municipal)

    async def queue_start_notice(call_id: str) -> None:
        try:
            await anyio.to_thread.run_sync(municipal.enqueue_start, call_id)
        except sqlite3.Error:
            return  # Recovery reconstructs the accepted call notice.
        if context.municipal is not None:
            await municipal.dispatch(context.municipal)

    async def deliver_call(turn: Row) -> None:
        await anyio.to_thread.run_sync(municipal.enqueue_turn, str(turn["session_id"]), str(turn["request_id"]))
        if context.municipal is not None:
            await municipal.dispatch(context.municipal)

    async def process_call(session: Row, turn: Row) -> None:
        await process_turn(context.search_database, services, session, turn)
        await deliver_call(turn)

    @router.post("/calls/hospital/retry")
    async def retry_hospital() -> dict[str, int]:
        raise HTTPException(409, "Hospital delivery requires municipality staff handoff")

    @router.post("/calls/municipality/retry")
    async def retry_municipality() -> dict[str, int]:
        if context.municipal is None:
            raise HTTPException(503, "Municipality intake is not configured")
        await anyio.to_thread.run_sync(municipal.recover)
        result = await municipal.dispatch(context.municipal)
        failed = await anyio.to_thread.run_sync(municipal.failed_count)
        response = {"delivered": result.delivered, "pending": result.pending}
        if failed:
            response["failed"] = failed
        return response

    @router.get("/calls")
    def list_calls() -> list[Row]:
        return calls.list()

    @router.post("/calls", status_code=201)
    def create_call(body: SessionInput, response: Response,
                    authorization: str | None = Header(default=None)) -> dict[str, str | bool | int]:
        service = context.capabilities.is_service(authorization)
        if not service and body.verification_token is not None:
            raise HTTPException(403, "Patient verification requires a trusted service")
        session = context.create_session(body)
        call_id = str(session["id"])
        calls.create(call_id)
        result = call_payload(get_call(call_id))
        if not service:
            result["call_token"] = context.capabilities.issue(call_id)
            response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/calls/{call_id}")
    def read_call(call_id: str) -> dict[str, str | bool | int]:
        return call_payload(get_call(call_id))

    @router.post("/calls/{call_id}/consent")
    def consent(call_id: str, body: ConsentInput, background: BackgroundTasks) -> dict[str, str | bool | int]:
        call = get_call(call_id)
        if bool(call.get("urgent")):
            raise HTTPException(409, "Urgent assistance is active")
        if call["state"] != "awaiting_consent":
            raise HTTPException(409, "Consent decision has already been recorded")
        if emergency_detected(body.text):
            calls.mark_urgent(call_id)
            return call_payload(get_call(call_id))
        decision, followup = split_consent(body.text)
        if decision != "ambiguous":
            if not calls.decide(call_id, decision):
                raise HTTPException(409, "Consent decision has already been recorded")
            if decision == "accepted":
                background.add_task(queue_start_notice, call_id)
        result = call_payload(get_call(call_id))
        if decision == 'accepted' and followup:
            result['followup_text'] = followup
        return result

    @router.post("/calls/{call_id}/interpret")
    async def interpret(call_id: str, body: IntentInput) -> dict[str, str | int | bool | dict[str, str | bool]]:
        require_active(call_id)
        if calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        # Explicit consent withdrawal is local and remains available even if a
        # delayed request carries a now-stale location epoch.
        local_decision = classify_private_decision(body.subject, body.text, [])
        if body.subject == 'caller':
            local_action = local_decision.intent
            if local_action == 'revoke_consent':
                if not await anyio.to_thread.run_sync(revoke_call, context.database, call_id):
                    raise HTTPException(409, 'Consent has already changed')
                return {'intent': local_action, 'applied': True, 'delivered_records_recallable': False}
            if local_action == 'revoke_location':
                changed = await anyio.to_thread.run_sync(recommendation_outbox.set_location_consent_with_epoch, call_id, False)
                if changed is None:
                    raise HTTPException(409, 'Consent has already changed')
                return {'intent': local_action, 'applied': True, 'location_generation': changed[2],
                        'location_revision': changed[3]}
        gate = await anyio.to_thread.run_sync(municipal.begin_interpretation, call_id) if body.subject == "caller" else None
        if body.subject == "caller" and gate is None:
            raise HTTPException(409, "Consent or call status changed")
        try:
            if body.subject == "caller" and body.request_id is not None:
                try:
                    admitted_utterance = await anyio.to_thread.run_sync(
                        utterances.admit, call_id, body.request_id, body.text, body.caller_context,
                        body.source_location_generation, body.source_location_revision)
                except DuplicateRequest as exc:
                    raise HTTPException(409, "Request ID already used with different input") from exc
                if not admitted_utterance:
                    raise HTTPException(409, "Consent or call status changed")
            generation = await anyio.to_thread.run_sync(location_generation, context.database, call_id)
            history = await anyio.to_thread.run_sync(context.database.history, call_id)
            provider = context.services.llm.decision_provider if isinstance(context.services.llm, SplitLLM) else context.services.llm
            admitted = await anyio.to_thread.run_sync(admit_conversation, context.database,
                ConversationScope(call_id, source_location_generation=body.source_location_generation,
                                  source_location_revision=body.source_location_revision,
                                  enforce_source_generation=True), history)
            if not admitted.active:
                raise HTTPException(409, "Consent or call status changed")
            prior_facts = ()
            admitted_history = admitted.history
            if body.subject == "caller" and body.request_id is not None:
                admitted = await anyio.to_thread.run_sync(admit_conversation, context.database,
                    ConversationScope(call_id, utterance_id=body.request_id,
                                      source_location_generation=body.source_location_generation,
                                      source_location_revision=body.source_location_revision,
                                      enforce_source_generation=True), history)
                if not admitted.active or admitted.facts is None:
                    raise HTTPException(409, "Consent or call status changed")
                snapshot = admitted.facts
                prior_facts = snapshot.current
                admitted_history = redact_superseded(admitted.history, snapshot.superseded,
                                                      snapshot.subject_excluded_utterance_ids)
            if body.subject == 'caller' and local_decision.intent in ('urgent', 'end'):
                decision = local_decision
            elif admitted.location_generation is None or not admitted.utterance_visible:
                decision = classify_private_decision(body.subject, body.text, admitted_history)
            else:
                decision = await classify_decision(provider, body.subject, body.text, admitted_history,
                                                caller_context=body.caller_context, recommendation=admitted.recommendation,
                                                facts=prior_facts)
            intent = decision.intent
            if body.subject == "caller" and body.request_id is not None:
                controls_only = decision.voice_controls is not None and decision.voice_controls.control_only
                expects_turn = intent == "consultation" and not decision.region_only and not controls_only
                classified = await anyio.to_thread.run_sync(utterances.classified, call_id, body.request_id,
                    intent, expects_turn, decision.location_subject == 'other', decision.patient_changed is True)
                if not classified:
                    raise HTTPException(409, "Location consent or call status changed")
                if intent in ("consultation", "recommend") and decision.facts:
                    try:
                        await anyio.to_thread.run_sync(apply_explicit, context.database, call_id,
                                                       body.request_id, decision.facts, generation)
                    except InvalidFact:
                        # Invalid model quotations must never become stored patient facts.
                        pass
            if intent == "revoke_consent":
                if not await anyio.to_thread.run_sync(revoke_call, context.database, call_id):
                    raise HTTPException(409, "Consent has already changed")
                return {"intent": intent, "applied": True, "delivered_records_recallable": False}
            if intent == "revoke_location":
                changed = await anyio.to_thread.run_sync(recommendation_outbox.set_location_consent_with_epoch, call_id, False)
                if changed is None:
                    raise HTTPException(409, "Consent has already changed")
                return {"intent": intent, "applied": True, "location_generation": changed[2],
                        "location_revision": changed[3]}
            if intent == "accept_location":
                changed = await anyio.to_thread.run_sync(partial(recommendation_outbox.set_location_consent_with_epoch,
                    call_id, True, source_location_generation=body.source_location_generation,
                    source_location_revision=body.source_location_revision))
                if changed is None:
                    raise HTTPException(409, "Location consent changed")
                return {"intent": intent, "applied": True, "location_generation": changed[2],
                        "location_revision": changed[3]}
            require_active(call_id)
            if calls.is_urgent(call_id):
                raise HTTPException(409, "Urgent assistance is active")
            if intent in ("recommend", "consultation") and body.subject == "caller":
                result: dict[str, str | bool | dict[str, str | bool]] = {"intent": intent}
                if decision.region_text and decision.region_text in body.text:
                    result["region_text"] = decision.region_text
                    result["region_only"] = decision.region_only is True
                if decision.location_subject is not None and (decision.location_subject != 'same_place' or explicit_same_place(body.text)):
                    result["location_subject"] = decision.location_subject
                if decision.patient_changed is not None:
                    result["patient_changed"] = decision.patient_changed
                if decision.voice_controls is not None:
                    result["voice_controls"] = decision.voice_controls.model_dump(exclude_none=True)
                if intent != "recommend":
                    return result
                specialty = explicit_specialty(body.text)
                result["recommendation_mode"] = decision.recommendation_mode or "new"
                if specialty is not None:
                    result["specialty"] = specialty
                return result
            return {"intent": intent}
        finally:
            if gate is not None:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(municipal.end_interpretation, gate)

    @router.post("/calls/{call_id}/revoke")
    async def revoke(call_id: str) -> dict[str, str | bool]:
        require_accepted(call_id)
        if not await anyio.to_thread.run_sync(revoke_call, context.database, call_id):
            raise HTTPException(409, "Consent has already changed")
        return {"state": "declined", "delivered_records_recallable": False}

    @router.post("/calls/{call_id}/urgent")
    async def urgent_call(call_id: str, background: BackgroundTasks, body: UrgentInput | None = None) -> dict[str, str | bool | int]:
        get_call(call_id)
        try:
            await anyio.to_thread.run_sync(calls.mark_urgent, call_id, body.text if body else None,
                                           body.source_location_generation if body else None,
                                           body.source_location_revision if body else None)
        except StaleLocationGeneration as exc:
            raise HTTPException(409, "Location consent changed") from exc
        background.add_task(queue_urgent_notice, call_id)
        return call_payload(get_call(call_id))

    @router.get("/calls/{call_id}/prompts/{kind}/audio")
    async def prompt_audio(call_id: str, kind: PromptKind) -> Response:
        get_call(call_id)
        if get_call(call_id).get("ended_at"):
            raise HTTPException(409, "Call has ended")
        if kind in ("ready", "question", "location_retry", "location_declined", "location_unavailable", "hospital_unavailable", "hospital_followup", "service_retry", "listening_retry", "intent_retry", "hold", "region_request", "region_ambiguous", "region_confirmed", "region_unavailable", "speed_slow", "speed_normal", "silence_check", "patient_reconfirm", "context_unavailable"):
            require_active(call_id)
            if calls.is_urgent(call_id):
                raise HTTPException(409, "Urgent assistance is active")
        response = await render_prompt(context.services.tts, kind, speed=call_speech_speed(context.database, call_id))
        if get_call(call_id).get("ended_at"):
            raise HTTPException(409, "Call has ended")
        if kind not in ("urgent", "pesticide_urgent") and calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        return response

    @router.post("/calls/{call_id}/messages", status_code=202, response_model=None)
    async def create_message(call_id: str, body: TurnInput, background: BackgroundTasks) -> Row:
        require_active(call_id)
        session = context.session_or_404(call_id)
        try:
            admitted = await anyio.to_thread.run_sync(calls.submit_turn, call_id, body)
        except StaleLocationGeneration as exc:
            raise HTTPException(409, "Location consent changed") from exc
        except DuplicateRequest as exc:
            raise HTTPException(409, "Request ID already used with different input") from exc
        if admitted is None:
            raise HTTPException(409, "Urgent assistance is active")
        turn, created, urgent = admitted
        if urgent:
            background.add_task(queue_urgent_notice, call_id)
            return {**visible_turn(turn), "urgent": True}
        if created:
            background.add_task(process_call, session, turn)
        return visible_turn(turn)

    @router.get("/calls/{call_id}/messages/{request_id}", response_model=None)
    def read_message(call_id: str, request_id: str, response: Response) -> dict[str, Value | dict[str, str | None]]:
        from app.audio_store import AudioStore
        get_call(call_id)
        turn = context.database.get_turn(call_id, request_id)
        if turn is None:
            raise HTTPException(404, "Turn not found")
        require_accepted(call_id)
        if calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        if not calls.refresh_turn_context(str(turn['id'])):
            refreshed = context.database.get_turn(call_id, request_id)
            if refreshed is None:
                raise HTTPException(404, 'Turn not found')
            turn = refreshed
        result = visible_turn(turn)
        audio = AudioStore(context.database).get(str(turn["id"]), "final")
        result["speech_text"] = audio.spoken_text if audio is not None else None
        response.headers["Cache-Control"] = "no-store"
        guidance = parse_guidance(turn)
        if guidance is not None and turn.get('status') not in ('interrupted', 'cancelled'):
            return {**result, 'guidance': guidance.model_dump()}
        return result

    @router.get("/calls/{call_id}/messages/{request_id}/audio/{kind}")
    def read_message_audio(call_id: str, request_id: str, kind: Literal["low", "final", "emergency"]) -> Response:
        from app.audio_store import AudioStore
        get_call(call_id)
        turn = context.database.get_turn(call_id, request_id)
        if turn is None:
            raise HTTPException(404, "Turn not found")
        require_accepted(call_id)
        if calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        if not calls.refresh_turn_context(str(turn['id'])):
            raise HTTPException(409, 'Question context is no longer valid')
        original_text = turn.get("initial_text" if kind == "low" else "final_text")
        if not isinstance(original_text, str) or not stored_audio_safe(original_text):
            raise HTTPException(409, "Legacy audio requires regeneration")
        result = AudioStore(context.database).get(str(turn["id"]), kind)
        if result is None:
            raise HTTPException(404, "Audio not available")
        return Response(content=result.data, media_type=result.mime_type,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    router.include_router(interrupt_router(context.database, municipal, context.municipal))
    router.include_router(recommendation_router(context, lambda: public_recommendations, lambda: add_audio))
    router.include_router(region_router(context))
    router.include_router(playback_router(context.database, municipal, context.municipal))
    router.include_router(urgent_followup_router(context.database, context.services.tts,
        getattr(context.services.llm, "decision_provider", context.services.llm), queue_urgent_notice))
    return router
