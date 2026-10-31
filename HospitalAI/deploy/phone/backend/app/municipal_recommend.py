"""Consent-gated municipal delivery of location-derived recommendations.

This adapter is deliberately not wired into the call API until the receiving
service implements generation tombstones and end-to-end revocation tests pass.
"""

from __future__ import annotations

import json
import socket
from contextlib import closing

import anyio
import httpx2
from pydantic import ValidationError

from app.db import Database
from app.hospital_bridge import DeliveryAck, DispatchResult, HospitalConfig
from app.recommend_dashboard import RecommendationOutbox
from app.recommend_payload import RecommendationPayload


class MunicipalRecommendationDispatch:
    """Deliver revocations before recommendations with replay-safe event IDs."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.outbox = RecommendationOutbox(database)

    def _next_revocation(self) -> tuple[str, str, str] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT r.event_id,r.call_id,r.body FROM municipal_location_revocations r
                JOIN calls c ON c.id=r.call_id WHERE r.state='pending' AND c.state='accepted'
                AND c.consent_version IN ('municipality-v2','municipality-v3') ORDER BY r.rowid LIMIT 1""").fetchone()
            return (str(row[0]), str(row[1]), str(row[2])) if row else None

    def _mark_revoked(self, event_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("UPDATE municipal_location_revocations SET state='sent' WHERE event_id=?", (event_id,))

    def _pending(self) -> int:
        with closing(self.database.connect()) as con:
            revocations = int(con.execute("SELECT count(*) FROM municipal_location_revocations WHERE state='pending'").fetchone()[0])
        return revocations + self.outbox._pending()

    def _next_recommendation(self) -> tuple[str, str, str, str, int] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT o.event_id,o.call_id,o.body,s.phone,g.generation
                FROM recommendation_outbox o JOIN calls c ON c.id=o.call_id
                JOIN sessions s ON s.id=c.id
                JOIN municipal_recommendation_generations g ON g.event_id=o.event_id
                JOIN call_location_consent l ON l.call_id=c.id
                WHERE o.state='pending' AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND c.urgent=0 AND l.state='accepted' AND l.generation=g.generation
                AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                    AND t.status IN ('emergency','pending','processing'))
                ORDER BY o.rowid LIMIT 1""").fetchone()
            return (str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4])) if row else None

    def _eligible(self, event_id: str, call_id: str, generation: int) -> bool:
        if not self.outbox._eligible(event_id, call_id):
            return False
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT 1 FROM calls c JOIN call_location_consent l ON l.call_id=c.id
                JOIN municipal_recommendation_generations g ON g.event_id=?
                WHERE c.id=? AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND c.urgent=0 AND l.state='accepted' AND l.generation=? AND g.generation=?""",
                (event_id, call_id, generation, generation)).fetchone()
            return row is not None

    async def dispatch(self, config: HospitalConfig, *, transport: httpx2.AsyncBaseTransport | None = None) -> DispatchResult:
        """Do not call until the receiver's tombstone and handoff contract is deployed."""
        delivered = 0
        limits = httpx2.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
        http_transport = transport or httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
        timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
        async with httpx2.AsyncClient(transport=http_transport, timeout=timeout,
                                      follow_redirects=False, trust_env=False) as client:
            while True:
                revocation = await anyio.to_thread.run_sync(self._next_revocation)
                if revocation is not None:
                    event_id, call_id, body = revocation
                    if not await self._post(client, config, event_id, call_id, body):
                        break
                    await anyio.to_thread.run_sync(self._mark_revoked, event_id)
                    delivered += 1
                    continue
                item = await anyio.to_thread.run_sync(self._next_recommendation)
                if item is None:
                    break
                event_id, call_id, body, phone, generation = item
                try:
                    preview = RecommendationPayload.model_validate_json(body)
                except ValidationError:
                    await anyio.to_thread.run_sync(self.outbox._mark_failed, event_id, "invalid_payload")
                    continue
                if preview.event_id != event_id or preview.call_id != call_id:
                    await anyio.to_thread.run_sync(self.outbox._mark_failed, event_id, "identity_mismatch")
                    continue
                payload = {"source": preview.source, "event_id": event_id, "call_id": call_id,
                    "occurred_at": preview.occurred_at.isoformat(), "event_type": "recommendation",
                    "phone": phone, "user_text": "", "assistant_text": "", "answer_status": None,
                    "sources": [], "location_generation": generation,
                    "recommendation": {"specialty": preview.specialty,
                        "hospitals": [hospital.model_dump(mode="json") for hospital in preview.hospitals],
                        "warning": preview.warning}}
                if not await anyio.to_thread.run_sync(self._eligible, event_id, call_id, generation):
                    continue
                if not await self._post(client, config, event_id, call_id,
                                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))):
                    break
                await anyio.to_thread.run_sync(self.outbox._mark_sent, event_id)
                delivered += 1
        return DispatchResult(delivered, await anyio.to_thread.run_sync(self._pending))

    @staticmethod
    async def _post(client: httpx2.AsyncClient, config: HospitalConfig, event_id: str,
                    call_id: str, body: str) -> bool:
        try:
            response = await client.post(f"{config.base_url.rstrip('/')}/api/municipality/intake",
                headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"},
                content=body.encode("utf-8"))
            if response.status_code not in (200, 201):
                return False
            ack = DeliveryAck.model_validate(response.json())
        except (httpx2.HTTPError, ValidationError, json.JSONDecodeError):
            return False
        created = response.status_code == 201 and ack.applied and not ack.duplicate
        replay = response.status_code == 200 and not ack.applied and ack.duplicate
        return (created or replay) and ack.event_id == event_id and ack.call_id == call_id
