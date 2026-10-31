"""Consent-gated, durable events for the municipal intake service."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlparse
from uuid import uuid4

import anyio
import httpx2
from pydantic import ValidationError

from app.db import Database
from app.hospital_bridge import DeliveryAck, DispatchResult
from app.services import EMERGENCY_MESSAGE
from app.stored_response import display_text, stored_audio_safe


class InvalidMunicipalConfig(Exception):
    """The intake destination is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class MunicipalConfig:
    base_url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        url = urlparse(self.base_url)
        loopback = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        if not ((url.scheme == "https" and url.hostname) or loopback):
            raise InvalidMunicipalConfig("Municipal intake must use HTTPS or loopback HTTP")
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/") or not self.token:
            raise InvalidMunicipalConfig("Municipal intake origin and token required")


class MunicipalOutbox:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_outbox (
                event_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                event_type TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sent')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_failures (
                event_id TEXT PRIMARY KEY REFERENCES municipal_outbox(event_id),
                status_code INTEGER NOT NULL,
                failed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_interruptions (
                event_id TEXT PRIMARY KEY REFERENCES municipal_outbox(event_id)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS municipal_interpretation_gates (
                id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                expires_at INTEGER NOT NULL
            )""")

    def begin_interpretation(self, call_id: str) -> str | None:
        """Block nonurgent sends during interpretation using an expiring per-request lease."""
        token = str(uuid4())
        with closing(self.database.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("DELETE FROM municipal_interpretation_gates WHERE expires_at <= unixepoch('now')")
            result = con.execute("""INSERT INTO municipal_interpretation_gates(id, call_id, expires_at)
                SELECT ?, id, unixepoch('now') + 60 FROM calls WHERE id=? AND state='accepted' AND urgent=0""",
                (token, call_id))
            return token if result.rowcount == 1 else None

    def end_interpretation(self, token: str) -> None:
        """Release only the lease belonging to the completed request."""
        with closing(self.database.connect()) as con, con:
            con.execute('DELETE FROM municipal_interpretation_gates WHERE id=?', (token,))

    def enqueue_start(self, call_id: str) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""SELECT s.phone, c.decision_at FROM calls c JOIN sessions s ON s.id=c.id
                WHERE c.id=? AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')""", (call_id,)).fetchone()
            if row is None:
                return False
            event = {"source": "hospital-ai", "event_id": f"start:{call_id}", "call_id": call_id,
                     "occurred_at": row["decision_at"], "event_type": "call_started", "phone": row["phone"],
                     "user_text": "", "assistant_text": "", "answer_status": None, "sources": []}
            return self._insert(con, event)

    def enqueue_urgent(self, call_id: str) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""SELECT s.phone, c.decision_at FROM calls c JOIN sessions s ON s.id=c.id
                WHERE c.id=? AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3') AND c.urgent=1""", (call_id,)).fetchone()
            if row is None:
                return False
            turn = con.execute("""SELECT input_text, final_text, updated_at FROM turns
                WHERE session_id=? AND status='emergency' ORDER BY rowid DESC LIMIT 1""", (call_id,)).fetchone()
            at = turn["updated_at"] if turn else con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
            event = {"source": "hospital-ai", "event_id": f"urgent:{call_id}", "call_id": call_id,
                     "occurred_at": at, "event_type": "urgent", "phone": row["phone"],
                     "user_text": str(turn["input_text"]) if turn else "",
                     "assistant_text": display_text(str(turn["final_text"])) if turn and turn["final_text"] else EMERGENCY_MESSAGE,
                     "answer_status": "emergency", "sources": []}
            return self._insert(con, event)

    def enqueue_turn(self, call_id: str, request_id: str) -> bool:
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""SELECT t.id, t.input_text, t.final_text, t.status, t.sources_json, t.updated_at, s.phone
                FROM turns t JOIN sessions s ON s.id=t.session_id JOIN calls c ON c.id=s.id
                WHERE c.id=? AND t.request_id=? AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND t.status IN ('completed','degraded') AND t.final_text IS NOT NULL""", (call_id, request_id)).fetchone()
            if row is None:
                return False
            refs = json.loads(row["sources_json"] or "[]")
            sources = [{key: str(ref.get(key, "")) for key in ("id", "type", "source")}
                       for ref in refs if isinstance(ref, dict)]
            event = {"source": "hospital-ai", "event_id": f"turn:{row['id']}", "call_id": call_id,
                     "occurred_at": row["updated_at"], "event_type": "turn_completed", "phone": row["phone"],
                     "user_text": row["input_text"], "assistant_text": display_text(str(row["final_text"])),
                     "answer_status": row["status"], "sources": sources}
            return self._insert(con, event)

    def enqueue_interrupted(self, call_id: str, request_id: str) -> bool:
        """Preserve the caller's words without treating the unheard answer as delivered."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            return self.insert_interrupted(con, call_id, request_id)

    @classmethod
    def insert_interrupted(cls, con, call_id: str, request_id: str) -> bool:
        row = con.execute("""SELECT t.id,t.input_text,t.updated_at,s.phone FROM turns t
            JOIN sessions s ON s.id=t.session_id JOIN calls c ON c.id=s.id
            WHERE c.id=? AND t.request_id=? AND c.state='accepted'
            AND c.consent_version IN ('municipality-v2','municipality-v3') AND t.status='interrupted'""",
            (call_id, request_id)).fetchone()
        if row is None:
            return False
        event = {"source": "hospital-ai", "event_id": f"interrupted:{row['id']}", "call_id": call_id,
                 "occurred_at": row["updated_at"], "event_type": "turn_interrupted", "phone": row["phone"],
                 "user_text": row["input_text"], "assistant_text": "응답이 중단됐습니다.",
                 "answer_status": "interrupted", "sources": []}
        return cls._insert(con, event)

    def enqueue_urgent_details(self, call_id: str) -> int:
        """Enqueue every distinct detail, including those arriving after the urgent flag."""
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("""SELECT d.text_hash, d.text, d.recorded_at, s.phone FROM urgent_detail_events d
                JOIN calls c ON c.id=d.call_id JOIN sessions s ON s.id=c.id
                WHERE c.id=? AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3') AND c.urgent=1""",
                (call_id,)).fetchall()
            count = 0
            legacy = con.execute("SELECT body FROM municipal_outbox WHERE event_id=?",
                                 (f"urgent-detail:{call_id}",)).fetchone()
            try:
                legacy_text = json.loads(legacy["body"]).get("user_text") if legacy else None
            except (json.JSONDecodeError, AttributeError):
                legacy_text = None
            for row in rows:
                if row["text"] == legacy_text:
                    continue
                event = {"source": "hospital-ai", "event_id": f"urgent-detail:{call_id}:{row['text_hash']}",
                         "call_id": call_id, "occurred_at": row["recorded_at"], "event_type": "urgent",
                         "phone": row["phone"], "user_text": row["text"], "assistant_text": EMERGENCY_MESSAGE,
                         "answer_status": "emergency", "sources": []}
                count += self._insert(con, event)
            return count

    @staticmethod
    def _insert(con, event: dict) -> bool:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        result = con.execute("INSERT OR IGNORE INTO municipal_outbox(event_id,call_id,event_type,body) VALUES(?,?,?,?)",
                             (event["event_id"], event["call_id"], event["event_type"], body))
        return result.rowcount == 1

    def recover(self) -> int:
        """Recover consented start, urgent and final events after interruption."""
        with closing(self.database.connect()) as con:
            calls = list(con.execute("""SELECT c.id, c.urgent,
                s.event_id IS NULL AS needs_start, u.event_id IS NULL AS needs_urgent
                FROM calls c LEFT JOIN municipal_outbox s ON s.event_id=('start:' || c.id)
                LEFT JOIN municipal_outbox u ON u.event_id=('urgent:' || c.id)
                WHERE c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND (s.event_id IS NULL OR (c.urgent=1 AND u.event_id IS NULL))"""))
            turns = list(con.execute("""SELECT t.session_id, t.request_id FROM turns t JOIN calls c ON c.id=t.session_id
                LEFT JOIN municipal_outbox o ON o.event_id=('turn:' || t.id)
                WHERE c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND t.status IN ('completed','degraded') AND t.final_text IS NOT NULL AND o.event_id IS NULL"""))
            interrupted = list(con.execute("""SELECT t.session_id,t.request_id FROM turns t
                JOIN calls c ON c.id=t.session_id
                LEFT JOIN municipal_outbox o ON o.event_id=('interrupted:' || t.id)
                WHERE c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND t.status='interrupted' AND o.event_id IS NULL"""))
            details = list(con.execute("""SELECT DISTINCT d.call_id FROM urgent_detail_events d
                JOIN calls c ON c.id=d.call_id
                LEFT JOIN municipal_outbox o ON o.event_id=('urgent-detail:' || d.call_id || ':' || d.text_hash)
                LEFT JOIN municipal_outbox legacy ON legacy.event_id=('urgent-detail:' || d.call_id)
                WHERE c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3') AND c.urgent=1
                AND o.event_id IS NULL AND (legacy.event_id IS NULL OR
                    CASE WHEN json_valid(legacy.body) THEN COALESCE(json_extract(legacy.body,'$.user_text'),'') != d.text ELSE 1 END)"""))
        count = sum(self.enqueue_start(str(row["id"])) for row in calls if row["needs_start"])
        count += sum(self.enqueue_urgent(str(row["id"])) for row in calls if row["urgent"] and row["needs_urgent"])
        count += sum(self.enqueue_urgent_details(str(row["call_id"])) for row in details)
        count += sum(self.enqueue_interrupted(str(row["session_id"]), str(row["request_id"])) for row in interrupted)
        return count + sum(self.enqueue_turn(str(row["session_id"]), str(row["request_id"])) for row in turns)

    def _next(self) -> tuple[str, str, str] | None:
        with closing(self.database.connect()) as con:
            row = con.execute("""SELECT o.event_id,o.call_id,o.body FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
                WHERE o.state='pending' AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
                AND (o.event_type='urgent' OR NOT EXISTS (SELECT 1 FROM municipal_interpretation_gates g
                    WHERE g.call_id=o.call_id AND g.expires_at > unixepoch('now')))
                AND (c.urgent=0 OR o.event_type='urgent' OR EXISTS (
                    SELECT 1 FROM municipal_outbox u WHERE u.event_id=('urgent:' || c.id) AND u.state='sent'))
                ORDER BY CASE o.event_type WHEN 'urgent' THEN 0 ELSE 1 END,o.rowid LIMIT 1""").fetchone()
            return (str(row["event_id"]), str(row["call_id"]), str(row["body"])) if row else None

    def _eligible(self, event_id: str, call_id: str) -> bool:
        with closing(self.database.connect()) as con:
            return con.execute("""SELECT 1 FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
                WHERE o.event_id=? AND o.call_id=? AND o.state='pending' AND c.state='accepted'
                AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)
                AND (o.event_type='urgent' OR NOT EXISTS (SELECT 1 FROM municipal_interpretation_gates g
                    WHERE g.call_id=o.call_id AND g.expires_at > unixepoch('now')))
                AND (c.urgent=0 OR o.event_type='urgent' OR EXISTS (
                    SELECT 1 FROM municipal_outbox u WHERE u.event_id=('urgent:' || c.id) AND u.state='sent'))""",
                (event_id, call_id)).fetchone() is not None

    def _mark_sent(self, event_id: str) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("UPDATE municipal_outbox SET state='sent' WHERE event_id=?", (event_id,))

    def _pending(self) -> int:
        with closing(self.database.connect()) as con:
            return int(con.execute("""SELECT count(*) FROM municipal_outbox o JOIN calls c ON c.id=o.call_id
                WHERE o.state='pending' AND c.state='accepted' AND c.consent_version IN ('municipality-v2','municipality-v3')
                AND NOT EXISTS (SELECT 1 FROM municipal_failures f WHERE f.event_id=o.event_id)
                AND NOT EXISTS (SELECT 1 FROM municipal_interruptions i WHERE i.event_id=o.event_id)""").fetchone()[0])

    def failed_count(self) -> int:
        with closing(self.database.connect()) as con:
            return int(con.execute("SELECT count(*) FROM municipal_failures").fetchone()[0])

    def _mark_failed(self, event_id: str, status_code: int) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("INSERT OR IGNORE INTO municipal_failures(event_id,status_code) VALUES (?,?)", (event_id, status_code))

    async def dispatch(self, config: MunicipalConfig) -> DispatchResult:
        delivered = 0
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(connect=5,read=15,write=10,pool=10),
                                      follow_redirects=False, trust_env=False) as client:
            while item := await anyio.to_thread.run_sync(self._next):
                event_id, call_id, body = item
                try:
                    event = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, 422)
                    continue
                if not isinstance(event, dict):
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, 422)
                    continue
                answer = event.get("assistant_text")
                if (event.get("event_id") != event_id or event.get("call_id") != call_id
                        or not isinstance(answer, str) or (answer and not stored_audio_safe(answer))):
                    await anyio.to_thread.run_sync(self._mark_failed, event_id, 422)
                    continue
                if not await anyio.to_thread.run_sync(self._eligible, event_id, call_id):
                    continue
                try:
                    response = await client.post(f"{config.base_url.rstrip('/')}/api/municipality/intake",
                        headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"},
                        content=body.encode("utf-8"))
                    if response.status_code in (400, 409, 413, 415, 422):
                        await anyio.to_thread.run_sync(self._mark_failed, event_id, response.status_code)
                        continue
                    if response.status_code not in (200, 201):
                        break
                    ack = DeliveryAck.model_validate(response.json())
                except (httpx2.HTTPError, ValidationError, json.JSONDecodeError):
                    break
                valid = (response.status_code == 201 and ack.applied and not ack.duplicate) or (
                    response.status_code == 200 and not ack.applied and ack.duplicate)
                if not valid or ack.event_id != event_id or ack.call_id != call_id:
                    break
                await anyio.to_thread.run_sync(self._mark_sent, event_id)
                delivered += 1
        return DispatchResult(delivered, await anyio.to_thread.run_sync(self._pending))
