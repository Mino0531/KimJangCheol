"""Call-scoped hospital lookup and location-consent endpoints."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol, TypeAlias

import anyio
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.call_consent import LocationConsentInput
from app.call_context import CallContext
from app.conversation_admission import ConversationScope, admit_conversation
from app.conversation_context import redact_superseded
from app.call_specialty import CallSpecialty, select_call_specialty
from app.call_speed import call_speech_speed
from app.call_store import CallStore
from app.db import Row
from app.orchestrator import SpeechOutput
from app.recommend_api import LocationInput, RecommendationResponse
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation import RecommendationQuery, RecommendationResult, recommend
from app.recommendation_navigation import (confirm as confirm_navigation, save as save_navigation,
                                           snapshot as navigation_snapshot, stage as stage_navigation,
                                           only_acknowledgements_since)
from app.services import emergency_detected
from app.region_resolution import public_region_index
from app.utterance_location import validate_source_generation

PublicLookup: TypeAlias = Callable[[RecommendationQuery, str], Awaitable[RecommendationResult]]


class RecommendationAudio(Protocol):
    def __call__(self, result: RecommendationResult, tts: SpeechOutput, timeout: float, *,
                 specialty_requested: bool = False, speed: float = 1.0,
                 location_precision: str = "gps", region_label: str | None = None) -> Awaitable[RecommendationResponse]: ...


class NavigationConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    navigation_token: str = Field(pattern=r"^[a-f0-9]{32}$")


class LocationContextReset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    source_location_generation: int | None = Field(default=None, ge=0, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)


def recommendation_router(context: CallContext,
                          public_lookup: Callable[[], PublicLookup],
                          audio: Callable[[], RecommendationAudio]) -> APIRouter:
    """Keep recommendation mutations separate from the call dialogue routes."""
    router = APIRouter()
    calls = CallStore(context.database)
    recommendation_outbox = RecommendationOutbox(context.database)

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

    async def require_location_generation(call_id: str, generation: int) -> None:
        require_active(call_id)
        current = await anyio.to_thread.run_sync(recommendation_outbox.location_generation, call_id)
        if current is None:
            raise HTTPException(403, "Location consent is required")
        if current != generation:
            raise HTTPException(409, "Location consent changed; request a new search")

    async def safe_history(call_id: str, body: LocationInput, generation: int) -> list[Row]:
        copied = await anyio.to_thread.run_sync(context.database.history, call_id)
        scope = ConversationScope(call_id, source_location_generation=body.source_location_generation,
                                  source_location_revision=body.source_location_revision,
                                  enforce_source_generation=True, include_all_facts=True)
        admitted = await anyio.to_thread.run_sync(admit_conversation, context.database, scope, copied)
        if not admitted.active or admitted.location_generation != generation:
            if calls.is_urgent(call_id):
                return []
            raise HTTPException(409, "Call or location consent changed")
        facts = admitted.facts
        if facts is None:
            return admitted.history
        return redact_superseded(admitted.history, facts.superseded, facts.subject_excluded_utterance_ids)

    @router.post("/calls/{call_id}/recommendations")
    async def get_recommendations(call_id: str, body: LocationInput, response: Response,
                                  background: BackgroundTasks) -> RecommendationResponse:
        require_active(call_id)
        generation = await anyio.to_thread.run_sync(recommendation_outbox.location_generation, call_id)
        if not body.location_consent or generation is None:
            raise HTTPException(403, "Location consent is required")
        with closing(context.database.connect()) as con:
            if not validate_source_generation(con, call_id, body.source_location_generation, body.source_location_revision):
                raise HTTPException(409, "Location consent changed; request a new search")
        if body.urgent or calls.is_urgent(call_id):
            response.headers["Cache-Control"] = "no-store"
            urgent_result = recommend(RecommendationQuery(latitude=body.latitude, longitude=body.longitude, urgent=True), [])
            return RecommendationResponse(**urgent_result.model_dump(), audio_status="unavailable" if body.include_audio else "not_requested")
        history = await safe_history(call_id, body, generation)
        urgent = body.urgent or calls.is_urgent(call_id) or any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in history)
        if not urgent and any(row["status"] in ("pending", "processing") for row in history):
            raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
        previous = await anyio.to_thread.run_sync(navigation_snapshot, context.database, call_id, generation)
        chosen = body.specialty or previous.chosen_specialty
        selection = select_call_specialty(chosen, history)
        mode = "new" if body.specialty and body.specialty != previous.chosen_specialty else body.mode
        if (mode in ("next", "repeat") and selection.source == "unknown" and not selection.urgent
                and previous.selected_specialty and previous.selection_source == "suggested"
                and only_acknowledgements_since(history, previous.last_turn_id)):
            selection = CallSpecialty(previous.selected_specialty, "suggested", "진료과 후보이며 의료진 확인이 필요합니다.")
        latitude, longitude = body.latitude, body.longitude
        if body.location_precision == "region_reference":
            resolved = public_region_index().resolve(body.region_label or "")
            if resolved.status != "matched" or resolved.match is None:
                raise HTTPException(422, "병원을 찾을 읍·면·동을 다시 확인해 주세요.")
            latitude, longitude = resolved.match.latitude, resolved.match.longitude
        elif context.capabilities.environment != "development" and not (urgent or selection.urgent):
            raise HTTPException(409, "기기 위치가 실제와 다를 수 있습니다. 검색할 읍·면·동을 확인해 주세요.")
        selection = CallSpecialty(None, "unknown", "확인한 지역의 종합병원을 안내합니다.", urgent=selection.urgent)
        query = RecommendationQuery(latitude=latitude, longitude=longitude, facility_type="general_hospital",
                                    urgent=urgent or selection.urgent)
        last_turn_id = str(history[-1]["id"]) if history else None
        if mode == "repeat" and not query.urgent and previous.current is not None and previous.current.facility_type == "general_hospital":
            result = previous.current or RecommendationResult(status="unavailable", warning="다시 안내할 병원이 없습니다.")
        else:
            await require_location_generation(call_id, generation)
            result = await public_lookup()(query, context.public_data_key)
            if result.status == "ok" and result.hospitals:
                # Do not present a known mismatch as the requested department, or
                # a dental clinic for a clearly non-dental limb complaint.
                limb = any(any(term in str(row.get("input_text") or "") for term in
                               ("무릎", "다리", "발목", "허리", "어깨", "손목")) for row in history)
                nursing_requested = any("요양병원" in str(row.get("input_text") or "") for row in history)
                unrelated = ("치과", "피부과", "안과", "이비인후과", "산부인과", "성형외과")
                hospitals = tuple(item for item in result.hospitals
                                  if not (selection.specialty and item.specialty_match == "no")
                                  and not ((selection.specialty and selection.specialty != "치과" or
                                            not selection.specialty and limb) and "치과" in item.name)
                                  and not (limb and selection.source != "chosen" and any(name in item.name for name in unrelated))
                                  and not (limb and not nursing_requested and "요양병원" in item.name))
                if hospitals:
                    result = result.model_copy(update={"hospitals": hospitals})
                else:
                    result = result.model_copy(update={"status": "unavailable", "hospitals": (),
                                                       "warning": "알맞은 주변 의료기관을 찾지 못했습니다."})
        seen = previous.seen
        current = previous.current
        if mode == "new":
            seen = ()
            current = None
        if mode == "next" and result.status == "ok":
            remaining = tuple(item for item in result.hospitals if item.id not in seen)
            if remaining:
                first = remaining[0]
                result = result.model_copy(update={"hospitals": (first,) + tuple(
                    item for item in result.hospitals if item.id != first.id)})
            else:
                result = result.model_copy(update={"status": "unavailable", "hospitals": (),
                    "warning": "안내할 다른 병원을 찾지 못했습니다."})
        if mode != "repeat" and result.status == "ok" and result.hospitals:
            seen = (*seen, result.hospitals[0].id)
            current = result
        if body.location_precision == "region_reference" and result.status == "ok":
            approximation = "지역 대표 위치를 기준으로 한 대략적인 거리입니다."
            warning = result.warning or ""
            if approximation not in warning:
                result = result.model_copy(update={"warning": f"{warning} {approximation}".strip()})
            if mode != "repeat":
                current = result
        await require_location_generation(call_id, generation)
        response.headers["Cache-Control"] = "no-store"
        delivery = "not_applicable"
        event_id = None
        try:
            entry = None if mode == "repeat" or body.include_audio else await anyio.to_thread.run_sync(partial(
                recommendation_outbox.enqueue, call_id, result, specialty=selection.specialty,
                location_consent=body.location_consent, expected_generation=generation))
            if entry is not None:
                event_id, state = entry
                delivery = ("pending" if context.municipal is not None else "disabled") if state == "pending" else state
            elif result.status == "ok" and result.hospitals:
                latest = await safe_history(call_id, body, generation)
                if any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in latest):
                    result = recommend(query.model_copy(update={"urgent": True}), [])
                elif any(row["status"] in ("pending", "processing") for row in latest):
                    raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
                else:
                    await require_location_generation(call_id, generation)
                    require_active(call_id)
        except sqlite3.Error:
            delivery = "failed"
        if body.include_audio:
            speed = await anyio.to_thread.run_sync(call_speech_speed, context.database, call_id)
            if speed == 1.0 and body.location_precision == "gps":
                prepared = await audio()(result, context.services.tts, context.services.tts_timeout,
                                         specialty_requested=selection.specialty is not None)
            else:
                prepared = await audio()(result, context.services.tts, context.services.tts_timeout,
                    specialty_requested=selection.specialty is not None, speed=speed,
                    location_precision=body.location_precision, region_label=body.region_label)
        else:
            prepared = RecommendationResponse(**result.model_dump())
        await require_location_generation(call_id, generation)
        latest = await safe_history(call_id, body, generation)
        if calls.is_urgent(call_id) or any(row["status"] == "emergency" or emergency_detected(str(row["input_text"])) for row in latest):
            if event_id is not None:
                await anyio.to_thread.run_sync(recommendation_outbox.cancel_event, call_id, event_id)
            urgent_result = recommend(query.model_copy(update={"urgent": True}), [])
            return RecommendationResponse(**urgent_result.model_dump(), audio_status="unavailable" if body.include_audio else "not_requested")
        if any(row["status"] in ("pending", "processing") for row in latest):
            if event_id is not None:
                await anyio.to_thread.run_sync(recommendation_outbox.cancel_event, call_id, event_id)
            raise HTTPException(409, "Wait for the current consultation before requesting recommendations")
        token = None
        if body.include_audio and mode != "repeat" and result.status == "ok" and result.hospitals:
            if prepared.audio_status == "synthesized" and current is not None:
                token = await anyio.to_thread.run_sync(stage_navigation, context.database, call_id,
                    previous, chosen, selection.specialty, selection.source, last_turn_id, seen, current)
                if token is None:
                    raise HTTPException(409, "Recommendation context changed; request it again")
        elif not body.include_audio and result.status != "urgent":
            saved = await anyio.to_thread.run_sync(save_navigation, context.database, call_id,
                previous, chosen, selection.specialty, selection.source, last_turn_id, seen, current)
            if not saved:
                if event_id is not None:
                    await anyio.to_thread.run_sync(recommendation_outbox.cancel_event, call_id, event_id)
                raise HTTPException(409, "Recommendation context changed; request it again")
        return prepared.model_copy(update={"navigation_token": token, "dashboard_delivery": delivery, "dashboard_event_id": event_id,
                                           "specialty": selection.specialty, "specialty_source": selection.source,
                                           "specialty_notice": selection.notice})

    @router.post("/calls/{call_id}/recommendations/confirm")
    async def confirm_recommendation(call_id: str, body: NavigationConfirmation) -> dict[str, str | bool]:
        require_active(call_id)
        confirmed = await anyio.to_thread.run_sync(confirm_navigation, context.database, call_id, body.navigation_token)
        if confirmed.outcome == "missing":
            raise HTTPException(404, "Recommendation confirmation not found")
        if confirmed.outcome == "conflict" or confirmed.result is None:
            raise HTTPException(409, "Recommendation context changed")
        await require_location_generation(call_id, confirmed.generation)
        entry = await anyio.to_thread.run_sync(partial(recommendation_outbox.enqueue, call_id, confirmed.result,
            specialty=confirmed.specialty, location_consent=True, expected_generation=confirmed.generation,
            event_id=f"rec:{body.navigation_token}"))
        if entry is None:
            return {"confirmed": True, "duplicate": confirmed.outcome == "duplicate", "delivery": "pending"}
        return {"confirmed": True, "duplicate": confirmed.outcome == "duplicate", "event_id": entry[0]}

    @router.post("/calls/{call_id}/location-consent")
    async def update_location_consent(call_id: str, body: LocationConsentInput) -> dict[str, str | int]:
        if body.accepted:
            require_active(call_id)
        else:
            require_accepted(call_id)
        result = await anyio.to_thread.run_sync(partial(recommendation_outbox.set_location_consent_with_epoch,
            call_id, body.accepted, source_location_generation=body.source_location_generation,
            source_location_revision=body.source_location_revision))
        if result is None:
            raise HTTPException(409, "Call or location consent changed")
        return {"state": result[0], "cancelled": result[1], "generation": result[2],
                "location_generation": result[2], "location_revision": result[3]}

    @router.post("/calls/{call_id}/location-context/reset")
    async def reset_location_context(call_id: str, body: LocationContextReset) -> dict[str, str | int]:
        require_active(call_id)
        epoch = await anyio.to_thread.run_sync(partial(recommendation_outbox.reset_location_generation_with_epoch,
            call_id, body.request_id, source_location_generation=body.source_location_generation,
            source_location_revision=body.source_location_revision))
        if epoch is None:
            raise HTTPException(409, "Location consent or call state changed")
        return {"state": "accepted", "generation": epoch[0], "location_generation": epoch[0],
                "location_revision": epoch[1]}

    @router.get("/calls/{call_id}/recommendations/{event_id}/delivery")
    def recommendation_delivery(call_id: str, event_id: str) -> dict[str, str]:
        require_accepted(call_id)
        state = recommendation_outbox.state(call_id, event_id)
        if state is None:
            raise HTTPException(404, "Recommendation not found")
        return {"state": (("pending" if context.municipal is not None else "disabled")
                          if state == "pending" else state)}

    return router
