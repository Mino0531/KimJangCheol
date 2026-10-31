"""HTTP transport for the legacy hospital recommendation outbox."""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import anyio
import httpx2
from pydantic import ValidationError

from app.hospital_bridge import DeliveryAck, DispatchResult, HospitalConfig
from app.recommend_payload import RecommendationPayload

if TYPE_CHECKING:
    from app.recommend_dashboard import RecommendationOutbox

async def dispatch_hospital(outbox: RecommendationOutbox, config: HospitalConfig, *, transport: httpx2.AsyncBaseTransport | None = None) -> DispatchResult:
    delivered = 0
    limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30)
    http_transport = transport or httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
    async with httpx2.AsyncClient(transport=http_transport, timeout=timeout,
                                  follow_redirects=False, trust_env=False) as client:
        while item := await anyio.to_thread.run_sync(outbox._next):
            event_id, call_id, body = item
            try:
                preview = RecommendationPayload.model_validate_json(body)
            except ValidationError:
                await anyio.to_thread.run_sync(outbox._mark_failed, event_id, "invalid_payload")
                continue
            if preview.event_id != event_id or preview.call_id != call_id:
                await anyio.to_thread.run_sync(outbox._mark_failed, event_id, "identity_mismatch")
                continue
            if not await anyio.to_thread.run_sync(outbox._eligible, event_id, call_id):
                break
            try:
                response = await client.post(f"{config.base_url.rstrip('/')}/api/team/recommendation-results",
                    headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"},
                    content=body.encode("utf-8"))
            except httpx2.HTTPError:
                break
            if response.status_code in (408, 429) or response.status_code >= 500:
                break
            if response.status_code not in (200, 201):
                await anyio.to_thread.run_sync(outbox._mark_failed, event_id, "http_rejected")
                continue
            try:
                ack = DeliveryAck.model_validate(response.json())
            except (ValidationError, json.JSONDecodeError):
                await anyio.to_thread.run_sync(outbox._mark_failed, event_id, "invalid_ack")
                continue
            created = response.status_code == 201 and ack.applied and not ack.duplicate
            replay = response.status_code == 200 and not ack.applied and ack.duplicate
            if not (created or replay) or ack.event_id != event_id or ack.call_id != call_id:
                await anyio.to_thread.run_sync(outbox._mark_failed, event_id, "invalid_ack")
                continue
            await anyio.to_thread.run_sync(outbox._mark_sent, event_id)
            delivered += 1
    return DispatchResult(delivered, await anyio.to_thread.run_sync(outbox._pending))
