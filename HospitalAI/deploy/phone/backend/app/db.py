"""SQLite persistence for sessions, retrieval sources, and reviewed records."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Final, Literal, TypeAlias
from uuid import uuid4

Value: TypeAlias = str | int | float | None
Row: TypeAlias = dict[str, Value]
WRITABLE: Final = frozenset({"status", "initial_text", "final_text", "sources_json", "timings_json", "tts_json", "error"})


class DuplicateRequest(Exception):
    """An idempotency key was reused with a different input."""


class InvalidCandidate(Exception):
    """The candidate is absent, unauthorized, or already reviewed."""


class InvalidTurnFields(Exception):
    """The requested update contains unsupported fields."""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self, seed: bool = False) -> None:
        with closing(self.connect()) as con, con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, phone TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS patient_phone ON patients(phone);
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, phone TEXT NOT NULL, patient_id TEXT REFERENCES patients(id),
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS turns (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
              request_id TEXT NOT NULL, input_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              initial_text TEXT, final_text TEXT, sources_json TEXT, timings_json TEXT, tts_json TEXT, error TEXT,
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              UNIQUE(session_id, request_id));
            CREATE TABLE IF NOT EXISTS patient_records (
              id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id), text TEXT NOT NULL,
              source TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS clinic_records (
              id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE TABLE IF NOT EXISTS candidates (
              id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
              patient_id TEXT NOT NULL REFERENCES patients(id), text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', reviewer TEXT, reviewed_at TEXT,
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
            CREATE INDEX IF NOT EXISTS patient_record_owner ON patient_records(patient_id);
            """)
            if seed:
                con.executemany("INSERT OR IGNORE INTO patients(id, phone) VALUES (?, ?)", [
                    ("sample-patient-1", "+821000000001"), ("sample-patient-2", "+821000000002")])
                con.executemany("INSERT OR IGNORE INTO patient_records(id, patient_id, text, source) VALUES (?, ?, ?, ?)", [
                    ("sample-history-1", "sample-patient-1", "고혈압 병력이 기록되어 있습니다.", "개발용 가상 환자 기록"),
                    ("sample-history-2", "sample-patient-2", "당뇨 병력이 기록되어 있습니다.", "개발용 가상 환자 기록")])
                con.executemany("INSERT OR IGNORE INTO clinic_records(id, text, source) VALUES (?, ?, ?)", [
                    ("sample-clinic-1", "기침, 발열과 감기 증상은 내과 상담 사유가 될 수 있습니다.", "개발용 가상 진료 사유"),
                    ("sample-clinic-2", "무릎 통증은 의료진의 진료가 필요한 상담 사유입니다.", "개발용 가상 진료 사유")])

    def find_patient_ids(self, phone: str) -> list[str]:
        with closing(self.connect()) as con:
            return [str(row[0]) for row in con.execute("SELECT id FROM patients WHERE phone = ?", (phone,))]

    def create_session(self, phone: str, patient_id: str | None = None) -> Row:
        session_id = str(uuid4())
        with closing(self.connect()) as con, con:
            if patient_id is not None:
                match = con.execute("SELECT id FROM patients WHERE id = ? AND phone = ?", (patient_id, phone)).fetchone()
                if match is None:
                    raise InvalidCandidate("Patient does not match the phone number")
            con.execute("INSERT INTO sessions(id, phone, patient_id) VALUES (?, ?, ?)", (session_id, phone, patient_id))
            return dict(con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def get_session(self, session_id: str) -> Row | None:
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row is not None else None

    def create_turn(self, session_id: str, request_id: str, text: str) -> tuple[Row, bool]:
        turn_id = str(uuid4())
        with closing(self.connect()) as con, con:
            cursor = con.execute("INSERT INTO turns(id, session_id, request_id, input_text) VALUES (?, ?, ?, ?) ON CONFLICT(session_id, request_id) DO NOTHING", (turn_id, session_id, request_id, text))
            row = con.execute("SELECT * FROM turns WHERE session_id = ? AND request_id = ?", (session_id, request_id)).fetchone()
            if row["input_text"] != text:
                raise DuplicateRequest(request_id)
            return dict(row), cursor.rowcount == 1

    def get_turn(self, session_id: str, request_id: str) -> Row | None:
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM turns WHERE session_id = ? AND request_id = ?", (session_id, request_id)).fetchone()
            return dict(row) if row is not None else None

    def update_turn(self, turn_id: str, fields: Row) -> None:
        if not fields or not fields.keys() <= WRITABLE:
            raise InvalidTurnFields("Invalid turn fields")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with closing(self.connect()) as con, con:
            con.execute(f"UPDATE turns SET {assignments}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (*fields.values(), turn_id))

    def history(self, session_id: str) -> list[Row]:
        with closing(self.connect()) as con:
            return [dict(row) for row in con.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY created_at, rowid", (session_id,))]

    def search(self, patient_id: str | None, query: str) -> list[Row]:
        terms = [term for term in re.findall(r"[가-힣A-Za-z0-9]+", query.casefold()) if len(term) > 1]
        grams = {term[i:i + 2] for term in terms for i in range(len(term) - 1)}
        if not grams:
            return []
        with closing(self.connect()) as con:
            rows = list(con.execute("SELECT id, text, source, updated_at, 'clinic' AS type FROM clinic_records"))
            if patient_id is not None:
                rows.extend(con.execute("SELECT id, text, source, updated_at, 'patient' AS type FROM patient_records WHERE patient_id = ?", (patient_id,)))
        scored = [(sum(gram in row["text"].casefold() for gram in grams), row) for row in rows]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [dict(id=f"{row['type']}:{row['id']}", type=row["type"], text=row["text"], source=row["source"], updated_at=row["updated_at"]) for score, row in scored if score > 0][:8]

    def create_candidate(self, turn_id: str, patient_id: str, text: str) -> Row:
        candidate_id = str(uuid4())
        with closing(self.connect()) as con, con:
            cursor = con.execute("""INSERT INTO candidates(id, turn_id, patient_id, text)
                SELECT ?, t.id, s.patient_id, ? FROM turns t JOIN sessions s ON s.id = t.session_id
                WHERE t.id = ? AND s.patient_id = ?""", (candidate_id, text, turn_id, patient_id))
            if cursor.rowcount != 1:
                raise InvalidCandidate("Turn does not belong to patient")
            return dict(con.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone())

    def candidates(self, patient_id: str | None = None) -> list[Row]:
        with closing(self.connect()) as con:
            if patient_id is None:
                rows = con.execute("SELECT * FROM candidates ORDER BY created_at, rowid")
            else:
                rows = con.execute("SELECT * FROM candidates WHERE patient_id = ? ORDER BY created_at, rowid", (patient_id,))
            return [dict(row) for row in rows]

    def review_candidate(self, candidate_id: str, decision: Literal["approved", "rejected"], reviewer: str) -> Row:
        if decision not in ("approved", "rejected") or not reviewer.strip():
            raise InvalidCandidate("Invalid decision or reviewer")
        with closing(self.connect()) as con, con:
            cursor = con.execute("""UPDATE candidates SET status = ?, reviewer = ?,
                reviewed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ? AND status = 'pending'""", (decision, reviewer, candidate_id))
            if cursor.rowcount != 1:
                raise InvalidCandidate("Candidate absent or already reviewed")
            if decision == "approved":
                con.execute("""INSERT INTO patient_records(id, patient_id, text, source)
                    SELECT ?, patient_id, text, ? FROM candidates WHERE id = ?""", (str(uuid4()), f"reviewed candidate:{candidate_id}", candidate_id))
            return dict(con.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone())
