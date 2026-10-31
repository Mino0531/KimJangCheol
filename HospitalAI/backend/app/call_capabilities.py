"""Short-lived, call-scoped credentials for the local patient interface."""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import time
from contextlib import closing
from hmac import compare_digest

from fastapi import HTTPException, Request

from app.db import Database


class CallCapabilities:
    """Issue local-only capabilities without exposing the integration token."""

    def __init__(self, database: Database, service_token: str, environment: str) -> None:
        self.database = database
        self.service_token = service_token
        self.environment = environment

    def initialize(self) -> None:
        with closing(self.database.connect()) as con, con:
            con.execute("""CREATE TABLE IF NOT EXISTS call_capabilities (
                token_hash TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)""")
            con.execute("CREATE INDEX IF NOT EXISTS capability_issued ON call_capabilities(issued_at)")
            con.execute("CREATE TABLE IF NOT EXISTS public_call_attempts (issued_at INTEGER NOT NULL)")

    def is_service(self, authorization: str | None) -> bool:
        return bool(self.service_token and authorization and
                    compare_digest(authorization, f"Bearer {self.service_token}"))

    def issue(self, call_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = int(time.time())
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO call_capabilities(token_hash, call_id, issued_at, expires_at) VALUES (?, ?, ?, ?)",
                        (digest, call_id, now, now + 3600))
        return token

    def refresh(self, call_id: str, authorization: str | None) -> str:
        """Issue a new capability only while the supplied call token is valid and active."""
        if not authorization or self.is_service(authorization) or not authorization.startswith("Bearer ") or len(authorization) > 160:
            raise HTTPException(401, "Authentication required")
        try:
            old_hash = hashlib.sha256(authorization[7:].encode("ascii")).hexdigest()
        except UnicodeEncodeError as exc:
            raise HTTPException(401, "Authentication required") from exc
        token = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = con.execute("""SELECT c.state,c.ended_at FROM call_capabilities k
                JOIN calls c ON c.id=k.call_id WHERE k.token_hash=? AND k.call_id=? AND k.expires_at>?""",
                (old_hash, call_id, now)).fetchone()
            if row is None:
                raise HTTPException(401, "Authentication required")
            if row["ended_at"] is not None or row["state"] not in ("awaiting_consent", "accepted"):
                raise HTTPException(409, "Call is not active")
            con.execute("INSERT INTO call_capabilities(token_hash,call_id,issued_at,expires_at) VALUES (?,?,?,?)",
                        (new_hash, call_id, now, now + 3600))
        return token

    def authorize(self, request: Request, authorization: str | None) -> None:
        if self.is_service(authorization):
            return
        if authorization:
            self._authorize_capability(request, authorization)
            return
        if self._local_call_creation(request):
            self._reserve_call_creation()
            return
        raise HTTPException(401, "Authentication required")

    def _authorize_capability(self, request: Request, authorization: str) -> None:
        call_id = request.path_params.get("call_id")
        if not isinstance(call_id, str):
            raise HTTPException(401, "Authentication required")
        self.authorize_call(call_id, authorization)

    def authorize_call(self, call_id: str, authorization: str) -> None:
        if not authorization.startswith("Bearer ") or len(authorization) > 160:
            raise HTTPException(401, "Authentication required")
        token = authorization[7:]
        try:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        except UnicodeEncodeError as exc:
            raise HTTPException(401, "Authentication required") from exc
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT 1 FROM call_capabilities WHERE token_hash = ? AND call_id = ? AND expires_at > ?",
                              (digest, call_id, int(time.time()))).fetchone()
        if row is None:
            raise HTTPException(401, "Authentication required")

    def _local_call_creation(self, request: Request) -> bool:
        if self.environment != "development" or request.method != "POST" or request.url.path != "/calls":
            return False
        if request.client is None or request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            return False
        try:
            loopback = ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            return False
        origin = request.headers.get("origin")
        return bool(loopback and origin == f"{request.url.scheme}://{request.headers.get('host')}" and
                    request.headers.get("content-type", "").split(";", 1)[0].lower() == "application/json")

    def _reserve_call_creation(self) -> None:
        """Limit even rejected local call creation before creating session records."""
        now = int(time.time())
        with closing(self.database.connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM public_call_attempts WHERE issued_at <= ?", (now - 60,))
            count = int(con.execute("SELECT count(*) FROM public_call_attempts").fetchone()[0])
            if count >= 20:
                raise HTTPException(429, "Too many call requests")
            con.execute("INSERT INTO public_call_attempts(issued_at) VALUES (?)", (now,))
