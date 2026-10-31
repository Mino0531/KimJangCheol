"""Refresh the current call's short-lived capability without interrupting requests."""

from typing import Annotated

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict

from app.call_capabilities import CallCapabilities


class RefreshedCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_token: str
    expires_in: int = 3600


def capability_refresh_router(capabilities: CallCapabilities) -> APIRouter:
    router = APIRouter()

    @router.post("/calls/{call_id}/capability/refresh")
    def refresh(call_id: str, response: Response,
                authorization: Annotated[str | None, Header()] = None) -> RefreshedCapability:
        token = capabilities.refresh(call_id, authorization)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return RefreshedCapability(call_token=token)

    return router
