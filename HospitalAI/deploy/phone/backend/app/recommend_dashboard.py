"""Consent-gated, replayable delivery of location-free recommendations."""

from __future__ import annotations

import hashlib
import json
import socket
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

import anyio
import httpx2
from pydantic import ValidationError

from app.db import Database
from app.hospital_bridge import DeliveryAck, DispatchResult, HospitalConfig
from app.recommend_payload import RecommendationPayload, valid_time
from app.recommendation import RecommendationResult
from app.services import emergency_detected


class RecommendationOutbox:
    """Stores a stable hospital payload without coordinates or generated audio."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS recommendation_outbox (
                event_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(state IN ('pending', 'sent')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS call_location_consent (
                call_id TEXT PRIMARY KEY REFERENCES calls(id),
                state TEXT NOT NULL CHECK(state IN ('accepted', 'revoked')),
                generation INTEGER NOT NULL CHECK(generation >= 0),
                changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS location_consent_events (
                id INTEGER PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                state TEXT NOT NULL, generation INTEGER NOT NULL,
                occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS recommendation_cancellations (
                event_id TEXT PRIMARY KEY REFERENCES recommendation_outbox(event_id),
                cancelled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS recommendation_failures (
                event_id TEXT PRIMARY KEY REFERENCES recommendation_outbox(event_id),
                reason TEXT NOT NULL,
                failed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_recommendation_generations (
                event_id TEXT PRIMARY KEY REFERENCES recommendation_outbox(event_id),
                generation INTEGER NOT NULL CHECK(generation > 0)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_location_revocations (
                event_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                generation INTEGER NOT NULL CHECK(generation > 0), body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'sent'))
            )""")

    def set_location_consent(self, call_id: str, accepted: bool) -> tuple[Literal["accepted", "revoked"], int] | None:
        """Record separate location consent and cancel queued unsent deliveries on withdrawal."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            call = con.execute("SELECT state,consent_version FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None or call["state"] != "accepted":
                return None
            row = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
            state: Literal["accepted", "revoked"] = "accepted" if accepted else "revoked"
            if row is not None and row["state"] == state:
                return state, 0
            generation = int(row["generation"]) if row is not None else 0
            if accepted:
                generation += 1
            con.execute("""INSERT INTO call_location_consent(call_id,state,generation) VALUES (?,?,?)
                ON CONFLICT(call_id) DO UPDATE SET state=excluded.state,generation=excluded.generation,
                changed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""", (call_id, state, generation))
            con.execute("INSERT INTO location_consent_events(call_id,state,generation) VALUES (?,?,?)",
                        (call_id, state, generation))
            cancelled = 0
            if not accepted:
                cursor = con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
                    SELECT event_id FROM recommendation_outbox o WHERE call_id=? AND state='pending'
                    AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)""", (call_id,))
                cancelled = cursor.rowcount
                if generation > 0 and call["consent_version"] in ("municipality-v2", "municipality-v3"):
                    session = con.execute("SELECT phone FROM sessions WHERE id=?", (call_id,)).fetchone()
                    event_id = f"location-revoked:{call_id}:{generation}"
                    body = json.dumps({"source": "hospital-ai", "event_id": event_id, "call_id": call_id,
                        "occurred_at": datetime.now(timezone.utc).isoformat(), "event_type": "location_revoked",
                        "phone": session["phone"], "user_text": "", "assistant_text": "", "answer_status": None,
                        "sources": [], "location_generation": generation}, ensure_ascii=False, separators=(",", ":"))
                    con.execute("""INSERT OR IGNORE INTO municipal_location_revocations
                        (event_id,call_id,generation,body) VALUES (?,?,?,?)""", (event_id, call_id, generation, body))
            return state, cancelled

    def location_accepted(self, call_id: str) -> bool:
        return self.location_generation(call_id) is not None

    def location_generation(self, call_id: str) -> int | None:
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
            return int(row["generation"]) if row is not None and row["state"] == "accepted" else None

    def enqueue(self, call_id: str, result: RecommendationResult, *, specialty: str | None,
                location_consent: bool, expected_generation: int | None = None) -> tuple[str, str] | None:
        if not location_consent or result.status != "ok" or not result.hospitals:
            return None
        hospitals = [{
            "name": item.name, "phone": item.phone, "distance_km": round(item.distance_km, 1),
            "specialist_count": item.specialist_count,
            "registered_schedule_status": item.registered_schedule_status,
            "hours": {day: pair for day, pair in (item.hours or {}).items()
                      if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
                      and len(pair) == 2 and valid_time(pair[0], end=False)
                      and valid_time(pair[1], end=True)} or None,
            "specialty_match": item.specialty_match, "source": item.source,
        } for item in result.hospitals[:3]]
        warning = ((result.warning or "") + " 병원 대시보드에는 상위 3곳만 전달됩니다.").strip()
        payload = {"call_id": call_id, "specialty": specialty, "hospitals": hospitals,
                   "warning": warning}
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            call = con.execute("SELECT state,urgent FROM calls WHERE id = ?", (call_id,)).fetchone()
            if call is None or call["state"] != "accepted" or call["urgent"]:
                return None
            consent = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
            if consent is None or consent["state"] != "accepted":
                return None
            if expected_generation is not None and int(consent["generation"]) != expected_generation:
                return None
            history = con.execute("SELECT status,input_text FROM turns WHERE session_id = ?", (call_id,))
            if any(row["status"] in ("emergency", "pending", "processing")
                   or emergency_detected(str(row["input_text"])) for row in history):
                return None
            canonical = json.dumps({"consent_generation": int(consent["generation"]), **payload},
                                   ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            event_id = "recommendation:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            body = json.dumps({"source": "hospital-ai", "event_id": event_id,
                               "occurred_at": datetime.now(timezone.utc).isoformat(), **payload},
                              ensure_ascii=False, separators=(",", ":"))
            con.execute("INSERT OR IGNORE INTO recommendation_outbox(event_id,call_id,body) VALUES (?,?,?)",
                        (event_id, call_id, body))
            con.execute("INSERT OR IGNORE INTO municipal_recommendation_generations(event_id,generation) VALUES (?,?)",
                        (event_id, int(consent["generation"])))
            row = con.execute("""SELECT o.state, EXISTS(SELECT 1 FROM recommendation_failures f
                WHERE f.event_id=o.event_id) AS failed FROM recommendation_outbox o
                WHERE o.event_id=? AND o.call_id=?""", (event_id, call_id)).fetchone()
            return event_id, "failed" if row["failed"] else str(row["state"])

    def cancel_event(self, call_id: str, event_id: str) -> None:
        """Cancel a queued event; an already-sent or in-flight request may remain at the receiver."""
        with closing(self.database.connect()) as con, con:
            con.execute("""INSERT OR IGNORE INTO recommendation_cancellations(event_id)
                SELECT event_id FROM recommendation_outbox o WHERE o.call_id=? AND o.event_id=?
                AND o.state='pending' AND NOT EXISTS (SELECT 1 FROM recommendation_failures f
                WHERE f.event_id=o.event_id)""", (call_id, event_id))

    def state(self, call_id: str, event_id: str) -> Literal["pending", "sent", "cancelled", "failed"] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT o.state, EXISTS(SELECT 1 FROM recommendation_cancellations c
                WHERE c.event_id=o.event_id) AS cancelled, EXISTS(SELECT 1 FROM recommendation_failures f
                WHERE f.event_id=o.event_id) AS failed FROM recommendation_outbox o
                WHERE o.event_id=? AND o.call_id=?""",
                              (event_id, call_id)).fetchone()
            if row is None:
                return None
            if row["state"] == "sent":
                return "sent"
            if row["cancelled"]:
                return "cancelled"
            return "failed" if row["failed"] else "pending"

    def _next(self) -> tuple[str, str, str] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT o.event_id,o.call_id,o.body FROM recommendation_outbox o
                JOIN calls c ON c.id=o.call_id WHERE o.state='pending' AND c.state='accepted' AND c.urgent=0
                AND EXISTS (SELECT 1 FROM call_location_consent l WHERE l.call_id=c.id AND l.state='accepted')
                AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id
                    AND t.status IN ('emergency','pending','processing'))
                ORDER BY o.rowid LIMIT 1""").fetchone()
            return (str(row[0]), str(row[1]), str(row[2])) if row else None

    def _mark_sent(self, event_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("UPDATE recommendation_outbox SET state='sent' WHERE event_id=?", (event_id,))

    def _mark_failed(self, event_id: str, reason: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("INSERT OR IGNORE INTO recommendation_failures(event_id,reason) VALUES (?,?)", (event_id, reason))

    def _pending(self) -> int:
        with closing(self.database.connect()) as con:
            return int(con.execute("""SELECT count(*) FROM recommendation_outbox o WHERE state='pending'
                AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)""").fetchone()[0])

    def _eligible(self, event_id: str, call_id: str) -> bool:
        """Recheck consent and urgent state immediately before the network request."""
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT 1 FROM recommendation_outbox o JOIN calls c ON c.id=o.call_id
                JOIN call_location_consent l ON l.call_id=c.id WHERE o.event_id=? AND o.call_id=?
                AND o.state='pending' AND c.state='accepted' AND c.urgent=0 AND l.state='accepted'
                AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)""",
                (event_id, call_id)).fetchone()
            if row is None:
                return False
            history = con.execute("SELECT status,input_text FROM turns WHERE session_id=?", (call_id,))
            return not any(item["status"] in ("emergency", "pending", "processing")
                           or emergency_detected(str(item["input_text"])) for item in history)

    async def dispatch(self, config: HospitalConfig, *, transport: httpx2.AsyncBaseTransport | None = None) -> DispatchResult:
        delivered = 0
        limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30)
        http_transport = transport or httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
        timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
        async with httpx2.AsyncClient(transport=http_transport, timeout=timeout,
                                      follow_redirects=False, trust_env=False) as client:
            while item := await anyio.to_thread.run_sync(self._next):
                event_id, call_id, body = item
                try:
                    preview = RecommendationPayload.model_validate_json(body)
                except ValidationError:
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, "invalid_payload")
                    continue
                if preview.event_id != event_id or preview.call_id != call_id:
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, "identity_mismatch")
                    continue
                if not await anyio.to_thread.run_sync(self._eligible, event_id, call_id):
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
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, "http_rejected")
                    continue
                try:
                    ack = DeliveryAck.model_validate(response.json())
                except (ValidationError, json.JSONDecodeError):
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, "invalid_ack")
                    continue
                created = response.status_code == 201 and ack.applied and not ack.duplicate
                replay = response.status_code == 200 and not ack.applied and ack.duplicate
                if not (created or replay) or ack.event_id != event_id or ack.call_id != call_id:
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, "invalid_ack")
                    continue
                await anyio.to_thread.run_sync(self._mark_sent, event_id)
                delivered += 1
        return DispatchResult(delivered, await anyio.to_thread.run_sync(self._pending))
