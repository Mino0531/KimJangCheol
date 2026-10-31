"""Consent-gated, replayable delivery of location-free recommendations."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal

import httpx2
from pydantic import ValidationError

from app.db import Database, DuplicateRequest
from app.hospital_bridge import DispatchResult, HospitalConfig
from app.recommend_payload import valid_time
from app.location_reset import enqueue_location_revocation, reset_location_generation, reset_location_generation_with_epoch
from app.location_consent import set_location_consent_with_epoch
from app.recommendation_hospital_dispatch import dispatch_hospital
from app.recommendation import RecommendationResult
from app.recommendation_turns import initialize_context_bindings
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
            con.execute("""CREATE TABLE IF NOT EXISTS call_location_resets (
                call_id TEXT NOT NULL REFERENCES calls(id), request_id TEXT NOT NULL,
                generation INTEGER NOT NULL, PRIMARY KEY(call_id,request_id))""")
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
            initialize_context_bindings(con)

    def set_location_consent(self, call_id: str, accepted: bool, *,
                             source_location_generation: int | None = None,
                             source_location_revision: int | None = None) -> tuple[Literal["accepted", "revoked"], int] | None:
        result = self.set_location_consent_with_generation(
            call_id, accepted, source_location_generation=source_location_generation,
            source_location_revision=source_location_revision)
        return (result[0], result[1]) if result is not None else None

    def set_location_consent_with_generation(self, call_id: str, accepted: bool, *,
                                            source_location_generation: int | None = None,
                                            source_location_revision: int | None = None) -> tuple[Literal["accepted", "revoked"], int, int] | None:
        result = self.set_location_consent_with_epoch(
            call_id, accepted, source_location_generation=source_location_generation,
            source_location_revision=source_location_revision)
        return (result[0], result[1], result[2]) if result is not None else None

    def set_location_consent_with_epoch(self, call_id: str, accepted: bool, *,
                                       source_location_generation: int | None = None,
                                       source_location_revision: int | None = None) -> tuple[Literal["accepted", "revoked"], int, int, int] | None:
        """Apply location consent with atomic generation and revision checks."""
        return set_location_consent_with_epoch(
            self.database, call_id, accepted, source_location_generation=source_location_generation,
            source_location_revision=source_location_revision)
    def location_accepted(self, call_id: str) -> bool:
        return self.location_generation(call_id) is not None

    def reset_location_generation(self, call_id: str, request_id: str, *,
                                  source_location_generation: int | None = None,
                                  source_location_revision: int | None = None) -> int | None:
        """Forget caller-device location after a patient-subject change, retaining consent."""
        return reset_location_generation(self.database, call_id, request_id, source_location_generation,
                                         source_location_revision)
    def reset_location_generation_with_epoch(self, call_id: str, request_id: str, *,
                                             source_location_generation: int | None = None,
                                             source_location_revision: int | None = None) -> tuple[int, int] | None:
        """Reset patient location and return the new generation and revision."""
        return reset_location_generation_with_epoch(
            self.database, call_id, request_id, source_location_generation, source_location_revision)
    def location_generation(self, call_id: str) -> int | None:
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT state,generation FROM call_location_consent WHERE call_id=?", (call_id,)).fetchone()
            return int(row["generation"]) if row is not None and row["state"] == "accepted" else None

    def enqueue(self, call_id: str, result: RecommendationResult, *, specialty: str | None,
                location_consent: bool, expected_generation: int | None = None,
                event_id: str | None = None, allow_confirmed_after_end: bool = False) -> tuple[str, str] | None:
        if not location_consent or result.status != "ok" or not result.hospitals:
            return None
        if event_id is not None and not re.fullmatch(r"rec:[A-Za-z0-9._-]{1,124}", event_id):
            raise DuplicateRequest("Invalid recommendation event ID")
        hospitals = [{
            "name": item.name, "phone": item.phone, "distance_km": round(item.distance_km, 1),
            "address": item.address[:500] if item.address else None,
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
            call = con.execute("SELECT state,urgent,ended_at FROM calls WHERE id = ?", (call_id,)).fetchone()
            if call is None or call["state"] != "accepted" or call["urgent"]:
                return None
            if call["ended_at"]:
                if not allow_confirmed_after_end or event_id is None or expected_generation is None:
                    return None
                table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_recommendation_pending'").fetchone()
                if table is None:
                    return None
                pending = con.execute("""SELECT result_json,selected_specialty FROM call_recommendation_pending
                    WHERE token=? AND call_id=? AND generation=? AND status='confirmed'""",
                    (event_id.removeprefix("rec:"), call_id, expected_generation)).fetchone()
                if pending is None:
                    return None
                try:
                    confirmed = RecommendationResult.model_validate_json(pending["result_json"])
                except ValidationError:
                    return None
                if confirmed != result or pending["selected_specialty"] != specialty:
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
            event_id = event_id or "recommendation:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            existing = con.execute("""SELECT o.call_id,o.body,g.generation FROM recommendation_outbox o
                LEFT JOIN municipal_recommendation_generations g ON g.event_id=o.event_id
                WHERE o.event_id=?""", (event_id,)).fetchone()
            if existing is not None:
                try:
                    previous = json.loads(existing["body"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise DuplicateRequest(event_id) from exc
                normalized_payload = json.loads(json.dumps(payload, ensure_ascii=False))
                if (existing["call_id"] != call_id or existing["generation"] != int(consent["generation"])
                        or any(previous.get(key) != value for key, value in normalized_payload.items())):
                    raise DuplicateRequest(event_id)
            body = json.dumps({"source": "hospital-ai", "event_id": event_id,
                               "occurred_at": datetime.now(timezone.utc).isoformat(), **payload},
                              ensure_ascii=False, separators=(",", ":"))
            con.execute("INSERT OR IGNORE INTO recommendation_outbox(event_id,call_id,body) VALUES (?,?,?)",
                        (event_id, call_id, body))
            con.execute("INSERT OR IGNORE INTO municipal_recommendation_generations(event_id,generation) VALUES (?,?)",
                        (event_id, int(consent["generation"])))
            row = con.execute("""SELECT o.state, EXISTS(SELECT 1 FROM recommendation_failures f
                WHERE f.event_id=o.event_id) AS failed, EXISTS(SELECT 1 FROM recommendation_cancellations x
                WHERE x.event_id=o.event_id) AS cancelled FROM recommendation_outbox o
                WHERE o.event_id=? AND o.call_id=?""", (event_id, call_id)).fetchone()
            return event_id, "failed" if row["failed"] else "cancelled" if row["cancelled"] else str(row["state"])

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
        """Deliver the existing outbox through the hospital transport."""
        return await dispatch_hospital(self, config, transport=transport)
