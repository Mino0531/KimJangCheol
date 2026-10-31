"""Trusted-proxy entry point for the unchanged HospitalAI phone application."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hmac import compare_digest
from ipaddress import ip_address

import anyio
import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.call_capabilities import CallCapabilities
from app.schemas import SessionInput


class GatewayConfigurationError(RuntimeError):
    """A required private gateway setting is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class GatewaySecrets:
    proxy: str = field(repr=False)
    service: str = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.proxy) < 32 or not self.service or compare_digest(self.proxy.encode(), self.service.encode()):
            raise GatewayConfigurationError("Distinct proxy and service secrets are required")


class CreatedCall(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    id: str


def trusted_creation(request: Request, secret: str) -> bool:
    """Require a private loopback proxy, exact origin, and JSON for creation."""
    if request.client is None:
        return False
    try:
        loopback = ip_address(request.client.host).is_loopback
    except ValueError:
        return False
    presented = request.headers.get("x-hospital-gateway", "")
    host = request.headers.get("host", "")
    origin = request.headers.get("origin", "")
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return bool(loopback and host and origin == f"{request.url.scheme}://{host}" and mime == "application/json"
                and compare_digest(presented.encode(), secret.encode()))


def build_gateway(phone: FastAPI, caps: CallCapabilities, secrets: GatewaySecrets) -> FastAPI:
    """Mount phone routes and safely issue call-scoped capabilities after proxy auth."""
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with phone.router.lifespan_context(phone):
            yield

    gateway = FastAPI(lifespan=lifespan)

    @gateway.post("/api/calls")
    async def create_public_call(request: Request, body: SessionInput) -> JSONResponse:
        if not trusted_creation(request, secrets.proxy) or body.verification_token is not None:
            raise HTTPException(403, "Call creation is not authorized")
        await anyio.to_thread.run_sync(caps._reserve_call_creation)
        transport = httpx2.ASGITransport(app=phone)
        async with httpx2.AsyncClient(transport=transport, base_url="http://phone.internal") as client:
            result = await client.post("/calls", json=body.model_dump(exclude_none=True),
                                       headers={"Authorization": f"Bearer {secrets.service}"})
        if result.status_code != 201:
            return JSONResponse(result.json(), status_code=result.status_code, headers={"Cache-Control": "no-store"})
        call = CreatedCall.model_validate(result.json())
        token = await anyio.to_thread.run_sync(caps.issue, call.id)
        payload = call.model_dump()
        payload["call_token"] = token
        return JSONResponse(payload, status_code=201, headers={"Cache-Control": "no-store"})

    gateway.mount("/api", phone)
    return gateway
