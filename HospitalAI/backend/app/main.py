"""Authenticated HTTP entry point for the consultation prototype."""

from __future__ import annotations

import re
from hmac import compare_digest
from os import getenv
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.audio_store import AudioStore
from app.azure_stt import speech_router
from app.call_api import CallContext, call_router
from app.call_capabilities import CallCapabilities
from app.call_capability_refresh import capability_refresh_router
from app.call_end import call_end_router
from app.call_search import CallDatabase
from app.call_speed import call_speed_router
from app.call_store import CallStore
from app.config import Settings, load_environment
from app.db import Database, DuplicateRequest, InvalidCandidate, Row
from app.hospital_bridge import HospitalConfig, HospitalOutbox
from app.municipal_bridge import MunicipalConfig, MunicipalOutbox
from app.municipal_retry import municipal_lifespan
from app.orchestrator import ServiceBundle, process_turn
from app.providers import build_services
from app.schemas import CandidateInput, ReviewInput, SessionInput, TurnInput
from app.stored_response import stored_audio_safe, visible_turn


class InvalidConfiguration(Exception):
    """A deployment configuration cannot safely be used."""


def normalize_phone(phone: str) -> str:
    compact = re.sub(r"[\s().-]", "", phone)
    if re.fullmatch(r"0[1-9]\d{7,10}", compact):
        compact = "+82" + compact[1:]
    if not re.fullmatch(r"\+[1-9]\d{7,14}", compact):
        raise HTTPException(422, "Use a Korean local number or an international + number")
    return compact


def create_app(settings: Settings, database: Database, services: ServiceBundle) -> FastAPI:
    if settings.environment != "development" and (settings.seed_demo or settings.dev_verification_token):
        raise InvalidConfiguration("Demo data and test verification are development-only")
    if settings.service_token and settings.admin_token and compare_digest(settings.service_token, settings.admin_token):
        raise InvalidConfiguration("Service and administrator tokens must differ")
    database.initialize(seed=settings.seed_demo)
    AudioStore(database).initialize()
    CallStore(database).initialize()
    call_capabilities = CallCapabilities(database, settings.service_token, settings.environment)
    call_capabilities.initialize()
    HospitalOutbox(database).initialize()
    municipal_outbox = MunicipalOutbox(database)
    municipal_outbox.initialize()
    municipal_outbox.recover()
    hospital = HospitalConfig(settings.hospital_url, settings.hospital_token) if (settings.hospital_url or settings.hospital_token) else None
    municipal = MunicipalConfig(settings.municipal_url, settings.municipal_token) if (settings.municipal_url or settings.municipal_token) else None
    application = FastAPI(title="Hospital AI consultation prototype", lifespan=municipal_lifespan(municipal_outbox, municipal))

    def authorize(expected: str, authorization: str | None) -> None:
        if not expected or not authorization or not compare_digest(authorization, f"Bearer {expected}"):
            raise HTTPException(401, "Authentication required")

    def service_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        authorize(settings.service_token, authorization)

    def admin_auth(authorization: Annotated[str | None, Header()] = None) -> None:
        authorize(settings.admin_token, authorization)

    def call_auth(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        call_capabilities.authorize(request, authorization)
        call_id = request.path_params.get("call_id")
        path = request.url.path
        root_path = request.scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        if not isinstance(call_id, str) or path in {
            f"/calls/{call_id}/end", f"/calls/{call_id}/revoke", f"/calls/{call_id}/location-consent"
        }:
            return
        processes_call = request.method not in ("GET", "HEAD", "OPTIONS") or "/prompts/" in path
        if processes_call:
            call = CallStore(database).get(call_id)
            if call is not None and call.get("ended_at"):
                raise HTTPException(409, "Call has ended")

    def session_or_404(session_id: str) -> Row:
        session = database.get_session(session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if settings.environment != "development" and session["patient_id"] is not None:
            raise HTTPException(403, "A production patient authentication adapter is required")
        return session

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/sessions", dependencies=[Depends(service_auth)])
    def create_session(payload: SessionInput) -> dict[str, str | bool]:
        phone = normalize_phone(payload.phone)
        ids = database.find_patient_ids(phone)
        verified = bool(
            settings.environment == "development"
            and settings.dev_verification_token
            and payload.verification_token
            and compare_digest(settings.dev_verification_token, payload.verification_token)
            and len(ids) == 1
        )
        session = database.create_session(phone, ids[0] if verified else None)
        return {"id": str(session["id"]), "authenticated": verified}

    @application.post("/sessions/{session_id}/turns", status_code=202, dependencies=[Depends(service_auth)])
    async def create_turn(session_id: str, payload: TurnInput, background: BackgroundTasks) -> Row:
        session = session_or_404(session_id)
        call = CallStore(database).get(session_id)
        if call is not None:
            raise HTTPException(403, "Use the consent-gated call messages endpoint")
        try:
            turn, created = database.create_turn(session_id, payload.request_id, payload.text)
        except DuplicateRequest as exc:
            raise HTTPException(409, "Request ID already used with different input") from exc
        if created:
            background.add_task(process_turn, database, services, session, turn)
        return visible_turn(turn)

    @application.get("/sessions/{session_id}/turns/{request_id}", dependencies=[Depends(service_auth)])
    def get_turn(session_id: str, request_id: str) -> Row:
        session_or_404(session_id)
        turn = database.get_turn(session_id, request_id)
        if turn is None:
            raise HTTPException(404, "Turn not found")
        return visible_turn(turn)

    @application.get("/sessions/{session_id}/turns/{request_id}/audio/{kind}", dependencies=[Depends(service_auth)])
    def get_audio(session_id: str, request_id: str, kind: str) -> Response:
        session_or_404(session_id)
        if kind not in ("low", "final", "emergency"):
            raise HTTPException(404, "Audio not found")
        turn = database.get_turn(session_id, request_id)
        if turn is None:
            raise HTTPException(404, "Turn not found")
        original_text = turn.get("initial_text" if kind == "low" else "final_text")
        if not isinstance(original_text, str) or not stored_audio_safe(original_text):
            raise HTTPException(409, "Legacy audio requires regeneration")
        audio = AudioStore(database).get(str(turn["id"]), kind)
        if audio is None:
            raise HTTPException(404, "Audio not available")
        return Response(content=audio.data, media_type=audio.mime_type,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @application.get("/sessions/{session_id}/history", dependencies=[Depends(service_auth)])
    def history(session_id: str) -> list[Row]:
        session_or_404(session_id)
        return [visible_turn(turn) for turn in database.history(session_id)]

    @application.post("/sessions/{session_id}/candidates", status_code=201, dependencies=[Depends(service_auth)])
    def create_candidate(session_id: str, payload: CandidateInput) -> Row:
        session = session_or_404(session_id)
        patient_id = session["patient_id"]
        if patient_id is None:
            raise HTTPException(403, "Verified patient required")
        if not any(turn["id"] == payload.turn_id for turn in database.history(session_id)):
            raise HTTPException(404, "Turn not found")
        try:
            return database.create_candidate(payload.turn_id, str(patient_id), payload.text)
        except InvalidCandidate as exc:
            raise HTTPException(400, "Invalid candidate") from exc

    @application.get("/sessions/{session_id}/candidates", dependencies=[Depends(service_auth)])
    def list_candidates(session_id: str) -> list[Row]:
        session = session_or_404(session_id)
        if session["patient_id"] is None:
            raise HTTPException(403, "Verified patient required")
        return database.candidates(str(session["patient_id"]), session_id=session_id)

    @application.post("/candidates/{candidate_id}/review", dependencies=[Depends(admin_auth)])
    def review(candidate_id: str, payload: ReviewInput) -> Row:
        try:
            return database.review_candidate(candidate_id, payload.decision, payload.reviewer)
        except InvalidCandidate as exc:
            raise HTTPException(409, "Candidate unavailable or already reviewed") from exc

    knowledge_path = Path(getenv("HOSPITAL_KNOWLEDGE_PATH", str(Path(__file__).resolve().parent.parent / "data" / "call_knowledge.json")))
    call_database = CallDatabase(database, knowledge_path, allow_unreviewed=settings.environment == "development")
    application.include_router(call_router(CallContext(
        database=database, search_database=call_database, services=services,
        auth=call_auth, session_or_404=session_or_404, create_session=create_session,
        capabilities=call_capabilities,
        public_data_key=settings.public_data_key,
        hospital=hospital,
        municipal=municipal,
    )))
    application.include_router(speech_router(settings, call_capabilities))
    application.include_router(capability_refresh_router(call_capabilities))
    application.include_router(call_speed_router(database, call_auth))
    application.include_router(call_end_router(database, call_auth))
    frontend = Path(__file__).resolve().parent.parent.parent / "frontend"
    if frontend.is_dir():
        application.mount("/ui", StaticFiles(directory=frontend, html=True), name="ui")
    return application


def default_app() -> FastAPI:
    load_environment()
    settings = Settings.from_env()
    return create_app(settings, Database(settings.database_path), build_services())


app = default_app()
