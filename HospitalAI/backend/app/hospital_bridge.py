"""Consent-gated, replayable delivery to the hospital dashboard."""

from __future__ import annotations

import json
import socket
from contextlib import closing
from dataclasses import dataclass, field
from os import getenv
from urllib.parse import urlparse

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from app.db import Database
from app.stored_response import display_text, stored_audio_safe


class InvalidHospitalConfig(Exception):
    """Hospital delivery was configured incompletely or unsafely."""


@dataclass(frozen=True, slots=True)
class HospitalConfig:
    base_url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        url = urlparse(self.base_url)
        loopback = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        if not ((url.scheme == "https" and url.hostname) or loopback):
            raise InvalidHospitalConfig("Dashboard must use HTTPS or loopback HTTP")
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise InvalidHospitalConfig("Dashboard URL must be an origin without credentials")
        if not self.token:
            raise InvalidHospitalConfig("Dashboard token is required")

    @classmethod
    def from_env(cls) -> HospitalConfig | None:
        url = getenv("HOSPITAL_DASHBOARD_URL", "")
        token = getenv("HOSPITAL_DASHBOARD_TOKEN", "")
        if not url and not token:
            return None
        return cls(url, token)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    delivered: int
    pending: int
    blocked_legacy: int = 0


class PendingPreview(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    assistant_text: str
    event_id: str
    call_id: str


class DeliveryAck(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    applied: bool
    duplicate: bool
    event_id: str
    call_id: str


class HospitalOutbox:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS hospital_outbox (
                event_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL REFERENCES calls(id),
                body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'sent')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")

    def enqueue_turn(self, session_id: str, request_id: str) -> bool:
        """Save a stable payload once, only for a consented final turn."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""SELECT t.id, t.input_text, t.final_text, t.status, t.sources_json,
                t.updated_at, s.phone FROM turns t JOIN sessions s ON s.id = t.session_id
                JOIN calls c ON c.id = s.id WHERE s.id = ? AND t.request_id = ?
                AND c.state = 'accepted' AND (c.urgent=0 OR t.status='emergency')
                AND t.status IN ('completed', 'degraded', 'emergency')
                AND t.final_text IS NOT NULL""", (session_id, request_id)).fetchone()
            if row is None:
                return False
            refs = json.loads(row["sources_json"] or "[]")
            sources = [{key: str(ref.get(key, "")) for key in ("id", "type", "source")}
                       for ref in refs if isinstance(ref, dict)]
            event_id = "turn:" + str(row["id"])
            body = json.dumps({
                "source": "hospital-ai", "event_id": event_id, "call_id": session_id,
                "occurred_at": row["updated_at"], "phone": row["phone"],
                "user_text": row["input_text"], "assistant_text": display_text(str(row["final_text"])),
                "answer_status": row["status"], "sources": sources,
            }, ensure_ascii=False, separators=(",", ":"))
            cursor = con.execute("INSERT OR IGNORE INTO hospital_outbox(event_id, call_id, body) VALUES (?, ?, ?)",
                                 (event_id, session_id, body))
            return cursor.rowcount == 1

    def recover(self) -> int:
        """Recover completed, consented turns missed before enqueue on process exit."""
        with closing(self.database.connect()) as con:
            rows = list(con.execute("""SELECT t.session_id, t.request_id FROM turns t
                JOIN calls c ON c.id = t.session_id
                LEFT JOIN hospital_outbox o ON o.event_id = ('turn:' || t.id)
                WHERE c.state = 'accepted' AND (c.urgent=0 OR t.status='emergency')
                AND t.status IN ('completed', 'degraded', 'emergency')
                AND t.final_text IS NOT NULL AND o.event_id IS NULL ORDER BY t.rowid"""))
        return sum(self.enqueue_turn(str(row["session_id"]), str(row["request_id"])) for row in rows)

    def _next(self) -> tuple[str, str, str] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT o.event_id, o.call_id, o.body FROM hospital_outbox o
                JOIN calls c ON c.id=o.call_id LEFT JOIN turns t ON t.session_id=o.call_id
                    AND ('turn:' || t.id)=o.event_id
                WHERE o.state='pending' AND c.state='accepted' AND (c.urgent=0 OR
                  (t.status='emergency' AND CASE WHEN json_valid(o.body)
                    THEN json_extract(o.body,'$.answer_status')='emergency' ELSE 0 END))
                ORDER BY o.rowid LIMIT 1""").fetchone()
            return (str(row["event_id"]), str(row["call_id"]), str(row["body"])) if row else None

    def _eligible(self, event_id: str, call_id: str) -> bool:
        """Recheck call consent and urgent state immediately before dispatch."""
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT 1 FROM hospital_outbox o JOIN calls c ON c.id=o.call_id
                LEFT JOIN turns t ON t.session_id=o.call_id AND ('turn:' || t.id)=o.event_id
                WHERE o.event_id=? AND o.call_id=? AND o.state='pending' AND c.state='accepted'
                AND (c.urgent=0 OR (t.status='emergency' AND CASE WHEN json_valid(o.body)
                  THEN json_extract(o.body,'$.answer_status')='emergency' ELSE 0 END))""",
                (event_id, call_id)).fetchone()
            return row is not None

    def _mark_sent(self, event_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("UPDATE hospital_outbox SET state = 'sent' WHERE event_id = ?", (event_id,))

    def _pending(self) -> int:
        with closing(self.database.connect()) as con:
            return int(con.execute("SELECT count(*) FROM hospital_outbox WHERE state = 'pending'").fetchone()[0])

    async def dispatch(self, config: HospitalConfig) -> DispatchResult:
        """Send in order; network or HTTP failures leave the exact payload for retry."""
        delivered = 0
        blocked_legacy = 0
        limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30.0)
        transport = httpx2.AsyncHTTPTransport(http2=True, retries=3, limits=limits,
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
        timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
        async with httpx2.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            while item := await anyio.to_thread.run_sync(self._next):
                event_id, call_id, body = item
                try:
                    pending = PendingPreview.model_validate_json(body)
                except ValidationError:
                    blocked_legacy = 1
                    break
                if (pending.event_id != event_id or pending.call_id != call_id
                        or not stored_audio_safe(pending.assistant_text)):
                    blocked_legacy = 1
                    break
                if not await anyio.to_thread.run_sync(self._eligible, event_id, call_id):
                    continue
                try:
                    response = await client.post(f"{config.base_url.rstrip('/')}/api/team/consultation-results",
                        headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"},
                        content=body.encode("utf-8"))
                except httpx2.HTTPError:
                    break
                if response.status_code not in (200, 201):
                    break
                try:
                    ack = DeliveryAck.model_validate(response.json())
                except (ValidationError, json.JSONDecodeError):
                    break
                expected_created = response.status_code == 201 and ack.applied and not ack.duplicate
                expected_replay = response.status_code == 200 and not ack.applied and ack.duplicate
                if not (expected_created or expected_replay) or ack.event_id != event_id or ack.call_id != call_id:
                    break
                await anyio.to_thread.run_sync(self._mark_sent, event_id)
                delivered += 1
        return DispatchResult(delivered, await anyio.to_thread.run_sync(self._pending), blocked_legacy)
