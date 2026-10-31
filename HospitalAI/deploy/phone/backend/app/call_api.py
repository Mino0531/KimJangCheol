"""Consent-gated call simulation and STT text interface."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Literal

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import Response

from app.call_consent import ConsentInput, LocationConsentInput, UrgentInput, consent_decision
from app.call_capabilities import CallCapabilities
from app.call_prompts import PromptKind, call_payload, render_prompt
from app.call_specialty import select_call_specialty
from app.call_store import CallStore
from app.call_interpret import IntentInput, classify_intent, explicit_specialty
from app.call_interrupt import interrupt_router
from app.call_playback import playback_router
from app.call_revocation import revoke_call
from app.db import Database, DuplicateRequest, Row
from app.hospital_bridge import HospitalConfig
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.orchestrator import ServiceBundle, process_turn
from app.recommend_dashboard import RecommendationOutbox
from app.recommend_api import LocationInput, RecommendationResponse, add_audio, public_recommendations
from app.recommendation import RecommendationQuery, recommend
from app.safe_demo_model import SafeDemoModel
from app.schemas import SessionInput, TurnInput
from app.services import MockLLM, SplitLLM, emergency_detected
from app.stored_response import stored_audio_safe, visible_turn

@dataclass(frozen=True, slots=True)
class CallContext:
    database: Database
    search_database: Database
    services: ServiceBundle
    auth: Callable[..., None]
    session_or_404: Callable[[str], Row]
    create_session: Callable[[SessionInput], dict[str, str | bool]]
    capabilities: CallCapabilities
    hospital: HospitalConfig | None = None
    municipal: MunicipalConfig | None = None
    public_data_key: str = field(default="", repr=False)


def call_router(context: CallContext) -> APIRouter:
    router = APIRouter(dependencies=[Depends(context.auth)])
    calls = CallStore(context.database)
    municipal = MunicipalOutbox(context.database)
    recommendation_outbox = RecommendationOutbox(context.database)
    recommendation_outbox.initialize()
    hospital = context.hospital
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

    async def require_location_generation(call_id: str, generation: int) -> None:
        current = await anyio.to_thread.run_sync(recommendation_outbox.location_generation, call_id)
        if current is None:
            raise HTTPException(403, "Location consent is required")
        if current != generation:
            raise HTTPException(409, "Location consent changed; request a new search")

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
                    authorization: str | None = Header(default=None)) -> dict[str, str | bool]:
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
    def read_call(call_id: str) -> dict[str, str | bool]:
        return call_payload(get_call(call_id))

    @router.post("/calls/{call_id}/recommendations")
    async def get_recommendations(call_id: str, body: LocationInput, response: Response,
                                  background: BackgroundTasks) -> RecommendationResponse:
        require_accepted(call_id)
        generation = await anyio.to_thread.run_sync(recommendation_outbox.location_generation, call_id)
        if not body.location_consent or generation is None:
            raise HTTPException(403, "Location consent is required")
        history = await anyio.to_thread.run_sync(context.database.history, call_id)
        urgent = body.urgent or calls.is_urgent(call_id) or any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in history)
        if not urgent and any(row["status"] in ("pending", "processing") for row in history):
            raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
        selection = select_call_specialty(body.specialty, history)
        query = RecommendationQuery(latitude=body.latitude, longitude=body.longitude,
                                    specialty=selection.specialty, urgent=urgent or selection.urgent)
        result = await public_recommendations(query, context.public_data_key)
        await require_location_generation(call_id, generation)
        response.headers["Cache-Control"] = "no-store"
        delivery = "not_applicable"
        event_id = None
        try:
            entry = await anyio.to_thread.run_sync(partial(recommendation_outbox.enqueue, call_id, result,
                                                           specialty=selection.specialty, location_consent=body.location_consent,
                                                           expected_generation=generation))
            if entry is not None:
                event_id, state = entry
                delivery = "disabled" if state == "pending" else state
            elif result.status == "ok" and result.hospitals:
                latest = await anyio.to_thread.run_sync(context.database.history, call_id)
                if any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in latest):
                    result = recommend(query.model_copy(update={"urgent": True}), [])
                elif any(row["status"] in ("pending", "processing") for row in latest):
                    raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
                else:
                    await require_location_generation(call_id, generation)
                    require_accepted(call_id)
        except sqlite3.Error:
            delivery = "failed"
        if body.include_audio:
            prepared = await add_audio(result, context.services.tts, context.services.tts_timeout,
                                       specialty_requested=selection.specialty is not None)
        else:
            prepared = RecommendationResponse(**result.model_dump())
        await require_location_generation(call_id, generation)
        latest = await anyio.to_thread.run_sync(context.database.history, call_id)
        if calls.is_urgent(call_id) or any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in latest):
            if event_id is not None:
                await anyio.to_thread.run_sync(recommendation_outbox.cancel_event, call_id, event_id)
            urgent_result = recommend(query.model_copy(update={"urgent": True}), [])
            return RecommendationResponse(**urgent_result.model_dump(), audio_status="unavailable" if body.include_audio else "not_requested")
        if any(row["status"] in ("pending", "processing") for row in latest):
            if event_id is not None:
                await anyio.to_thread.run_sync(recommendation_outbox.cancel_event, call_id, event_id)
            raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
        return prepared.model_copy(update={"dashboard_delivery": delivery, "dashboard_event_id": event_id,
                                           "specialty": selection.specialty, "specialty_source": selection.source,
                                           "specialty_notice": selection.notice})

    @router.post("/calls/{call_id}/location-consent")
    async def update_location_consent(call_id: str, body: LocationConsentInput) -> dict[str, str | int]:
        require_accepted(call_id)
        result = await anyio.to_thread.run_sync(recommendation_outbox.set_location_consent, call_id, body.accepted)
        if result is None:
            raise HTTPException(403, "Call consent is required")
        return {"state": result[0], "cancelled": result[1]}

    @router.get("/calls/{call_id}/recommendations/{event_id}/delivery")
    def recommendation_delivery(call_id: str, event_id: str) -> dict[str, str]:
        require_accepted(call_id)
        state = recommendation_outbox.state(call_id, event_id)
        if state is None:
            raise HTTPException(404, "Recommendation not found")
        return {"state": "disabled" if state == "pending" else state}

    @router.post("/calls/{call_id}/consent")
    def consent(call_id: str, body: ConsentInput, background: BackgroundTasks) -> dict[str, str | bool]:
        call = get_call(call_id)
        if bool(call.get("urgent")):
            raise HTTPException(409, "Urgent assistance is active")
        if call["state"] != "awaiting_consent":
            raise HTTPException(409, "Consent decision has already been recorded")
        if emergency_detected(body.text):
            calls.mark_urgent(call_id)
            return call_payload(get_call(call_id))
        decision = consent_decision(body.text)
        if decision != "ambiguous":
            if not calls.decide(call_id, decision):
                raise HTTPException(409, "Consent decision has already been recorded")
            if decision == "accepted":
                background.add_task(queue_start_notice, call_id)
        return call_payload(get_call(call_id))

    @router.post("/calls/{call_id}/interpret")
    async def interpret(call_id: str, body: IntentInput) -> dict[str, str | bool]:
        require_accepted(call_id)
        if calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        gate = await anyio.to_thread.run_sync(municipal.begin_interpretation, call_id) if body.subject == "caller" else None
        if body.subject == "caller" and gate is None:
            raise HTTPException(409, "Consent or call status changed")
        try:
            history = await anyio.to_thread.run_sync(context.database.history, call_id)
            provider = context.services.llm.low_provider if isinstance(context.services.llm, SplitLLM) else context.services.llm
            intent = await classify_intent(provider, body.subject, body.text, history)
            require_accepted(call_id)
            if calls.is_urgent(call_id):
                raise HTTPException(409, "Urgent assistance is active")
            if intent == "revoke_consent":
                if not await anyio.to_thread.run_sync(revoke_call, context.database, call_id):
                    raise HTTPException(409, "Consent has already changed")
                return {"intent": intent, "applied": True, "delivered_records_recallable": False}
            if intent == "revoke_location":
                changed = await anyio.to_thread.run_sync(recommendation_outbox.set_location_consent, call_id, False)
                if changed is None:
                    raise HTTPException(409, "Consent has already changed")
                return {"intent": intent, "applied": True}
            if intent == "recommend" and body.subject == "caller":
                specialty = explicit_specialty(body.text)
                if specialty is not None:
                    return {"intent": intent, "specialty": specialty}
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
    async def urgent_call(call_id: str, background: BackgroundTasks, body: UrgentInput | None = None) -> dict[str, str | bool]:
        get_call(call_id)
        await anyio.to_thread.run_sync(calls.mark_urgent, call_id, body.text if body else None)
        background.add_task(queue_urgent_notice, call_id)
        return call_payload(get_call(call_id))

    @router.get("/calls/{call_id}/prompts/{kind}/audio")
    async def prompt_audio(call_id: str, kind: PromptKind) -> Response:
        get_call(call_id)
        if kind in ("ready", "question", "location_retry", "location_declined", "location_unavailable", "hospital_unavailable", "intent_retry"):
            require_accepted(call_id)
            if calls.is_urgent(call_id):
                raise HTTPException(409, "Urgent assistance is active")
        response = await render_prompt(context.services.tts, kind)
        if kind not in ("urgent", "pesticide_urgent") and calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        return response

    @router.post("/calls/{call_id}/messages", status_code=202, response_model=None)
    async def create_message(call_id: str, body: TurnInput, background: BackgroundTasks) -> Row:
        require_accepted(call_id)
        session = context.session_or_404(call_id)
        try:
            admitted = await anyio.to_thread.run_sync(calls.submit_turn, call_id, body)
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

    @router.get("/calls/{call_id}/messages/{request_id}")
    def read_message(call_id: str, request_id: str, response: Response) -> Row:
        from app.audio_store import AudioStore
        get_call(call_id)
        turn = context.database.get_turn(call_id, request_id)
        if turn is None:
            raise HTTPException(404, "Turn not found")
        require_accepted(call_id)
        if calls.is_urgent(call_id):
            raise HTTPException(409, "Urgent assistance is active")
        result = visible_turn(turn)
        audio = AudioStore(context.database).get(str(turn["id"]), "final")
        result["speech_text"] = audio.spoken_text if audio is not None else None
        response.headers["Cache-Control"] = "no-store"
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
        original_text = turn.get("initial_text" if kind == "low" else "final_text")
        if not isinstance(original_text, str) or not stored_audio_safe(original_text):
            raise HTTPException(409, "Legacy audio requires regeneration")
        result = AudioStore(context.database).get(str(turn["id"]), kind)
        if result is None:
            raise HTTPException(404, "Audio not available")
        return Response(content=result.data, media_type=result.mime_type,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    router.include_router(interrupt_router(context.database, municipal, context.municipal))
    router.include_router(playback_router(context.database, municipal, context.municipal))
    return router
