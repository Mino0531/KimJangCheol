"""Consent-scoped caller quotations ordered by their source utterance."""

from contextlib import closing, nullcontext
from dataclasses import dataclass
import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db import Database

FactKey = Literal['symptom', 'onset', 'specialty', 'caller_role', 'preparation']


class InvalidFact(Exception):
    """Model output did not quote the caller's actual utterance."""


class FactUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    key: FactKey
    quote: str = Field(min_length=1, max_length=250)
    operation: Literal['add', 'replace']


@dataclass(frozen=True, slots=True)
class FactEvidence:
    key: FactKey
    quote: str
    utterance_id: str
    status: Literal['reported'] = 'reported'


@dataclass(frozen=True, slots=True)
class FactSnapshot:
    current: tuple[FactEvidence, ...]
    superseded: tuple[FactEvidence, ...]
    subject_excluded_utterance_ids: tuple[str, ...] = ()
    patient_subject_changed: bool = False


def _location_independent(item: FactUpdate) -> bool:
    """Accept only tightly bounded, location-free quotations as independent.

    This is a privacy check on model-selected quotations, not a symptom or
    clinical classifier. Unknown words fail closed to the location generation.
    """
    quote = ' '.join(item.quote.split())
    if item.key == 'symptom':
        return bool(re.fullmatch(r'(?:무릎|허리|어깨|머리|배|다리|팔|손목|발목|기침|가래|콧물|열|목|눈|귀|이|잇몸|치아|가슴|숨)(?:이|가|은|는)?\s*(?:아파요|아픕니다|아파|아프다|아프세요|불편해요|불편합니다|불편하다|나요|납니다|나|납니다|납니다|나와요|나요|차요|차다|아팠어요|아팠습니다|없어요|안나요|안 나요)', quote) or
                    re.fullmatch(r'(?:어지러워요|어지럽습니다|어지러워|어질어질해요|어질어질합니다|토했어요|구토했어요)', quote))
    if item.key == 'onset':
        return bool(re.fullmatch(r'(?:오늘|어제|그저께|며칠 전|지난주|이번 주|지난달|방금|아까|이틀 전|사흘 전)(?:부터|에)?(?:\s*(?:시작했어요|시작됐어요|시작했습니다|아팠어요))?', quote))
    if item.key == 'specialty':
        return quote in {'내과', '정형외과', '신경과', '신경외과', '가정의학과', '외과', '치과', '안과', '이비인후과', '피부과', '산부인과', '비뇨의학과', '재활의학과', '소아청소년과', '한의과'}
    if item.key == 'caller_role':
        return quote in {'본인', '저', '어머니', '아버지', '부모님', '남편', '아내', '할머니', '할아버지', '딸', '아들', '보호자', '가족'}
    return quote in {'신분증', '약봉투', '복용약', '건강보험증'}


def initialize(database: Database) -> None:
    """Create append-only, per-call facts without modifying existing turns."""
    with closing(database.connect()) as con, con:
        con.execute('''CREATE TABLE IF NOT EXISTS call_fact_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL REFERENCES calls(id),
            utterance_id TEXT NOT NULL, source_sequence INTEGER NOT NULL,
            fact_key TEXT NOT NULL, quote TEXT NOT NULL, operation TEXT NOT NULL,
            location_generation INTEGER NOT NULL,
            UNIQUE(call_id,utterance_id,fact_key,quote,operation))''')
        con.execute('''CREATE TABLE IF NOT EXISTS call_fact_applied (
            call_id TEXT NOT NULL REFERENCES calls(id), utterance_id TEXT NOT NULL,
            PRIMARY KEY(call_id,utterance_id))''')
        con.execute('''CREATE TABLE IF NOT EXISTS call_fact_subject_boundaries (
            call_id TEXT NOT NULL REFERENCES calls(id), utterance_id TEXT NOT NULL,
            source_sequence INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(call_id,utterance_id))''')
        con.execute('''CREATE TABLE IF NOT EXISTS call_fact_tombstones (
            call_id TEXT NOT NULL REFERENCES calls(id), source_sequence INTEGER NOT NULL,
            source_event_id INTEGER NOT NULL, fact_key TEXT NOT NULL,
            PRIMARY KEY(call_id,source_event_id))''')


def invalidate_location_facts(con: sqlite3.Connection, call_id: str) -> None:
    """Remove potentially location-bearing facts within the revocation transaction."""
    table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_events'").fetchone()
    if table is not None:
        con.execute('''CREATE TABLE IF NOT EXISTS call_fact_tombstones (
            call_id TEXT NOT NULL REFERENCES calls(id), source_sequence INTEGER NOT NULL,
            source_event_id INTEGER NOT NULL, fact_key TEXT NOT NULL,
            PRIMARY KEY(call_id,source_event_id))''')
        con.execute('''INSERT OR IGNORE INTO call_fact_tombstones
            (call_id,source_sequence,source_event_id,fact_key)
            SELECT call_id,source_sequence,id,fact_key FROM call_fact_events
            WHERE call_id=? AND location_generation>0 AND operation='replace' ''', (call_id,))
        con.execute('DELETE FROM call_fact_events WHERE call_id=? AND location_generation>0', (call_id,))


def mark_patient_subject_change(con: sqlite3.Connection, call_id: str, utterance_id: str) -> bool:
    """Record the switch to another patient's history in the caller's transaction."""
    cursor = con.execute('''INSERT OR IGNORE INTO call_fact_subject_boundaries
        (call_id,utterance_id,source_sequence,created_at)
        SELECT u.call_id,u.request_id,u.seq,u.created_at FROM pending_utterances u
        JOIN calls c ON c.id=u.call_id WHERE u.call_id=? AND u.request_id=? AND u.state='accepted'
        AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL''', (call_id, utterance_id))
    return cursor.rowcount == 1 or con.execute('''SELECT 1 FROM call_fact_subject_boundaries
        WHERE call_id=? AND utterance_id=?''', (call_id, utterance_id)).fetchone() is not None


def location_generation(database: Database, call_id: str) -> int | None:
    """Capture the location permission before calling the model."""
    with closing(database.connect()) as con:
        row = con.execute("""SELECT l.generation FROM calls c JOIN call_location_consent l ON l.call_id=c.id
            WHERE c.id=? AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL
            AND l.state='accepted'""", (call_id,)).fetchone()
        return int(row['generation']) if row else None


def apply_explicit(database: Database, call_id: str, utterance_id: str,
                   updates: list[FactUpdate], expected_location_generation: int | None) -> bool:
    """Store only validated quotations from an accepted caller utterance.

    Source ordering rather than model completion ordering resolves corrections.
    Every item is conservatively tied to the permission generation because an
    utterance can contain location information alongside another fact.
    """
    if len(updates) > 10:
        return False
    with closing(database.connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("""SELECT u.seq,u.input_text,l.state AS location_state,l.generation FROM pending_utterances u
            JOIN calls c ON c.id=u.call_id LEFT JOIN call_location_consent l ON l.call_id=c.id
            WHERE u.call_id=? AND u.request_id=? AND u.state='accepted'
            AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL""",
            (call_id, utterance_id)).fetchone()
        if row is None:
            return False
        current_generation = int(row['generation']) if row['location_state'] == 'accepted' else None
        if current_generation != expected_location_generation:
            return False
        text = str(row['input_text'])
        if any(not item.quote.strip() or item.quote not in text for item in updates):
            raise InvalidFact('Fact must quote the source utterance')
        inserted = con.execute('''INSERT OR IGNORE INTO call_fact_applied(call_id,utterance_id)
            VALUES (?,?)''', (call_id, utterance_id))
        if inserted.rowcount != 1:
            return True
        for item in updates:
            dependent_generation = 0 if _location_independent(item) else current_generation
            if dependent_generation is None:
                continue
            con.execute('''INSERT OR IGNORE INTO call_fact_events
                (call_id,utterance_id,source_sequence,fact_key,quote,operation,location_generation)
                VALUES (?,?,?,?,?,?,?)''', (call_id, utterance_id, int(row['seq']), item.key,
                                              item.quote, item.operation, dependent_generation))
        return True


def read_current(database: Database, call_id: str, *,
                 through_utterance_id: str | None = None) -> list[FactEvidence]:
    """Read current quotations only through an optional source utterance."""
    return list(read_snapshot(database, call_id, through_utterance_id=through_utterance_id).current)


def read_snapshot(database: Database, call_id: str, *,
                  through_utterance_id: str | None = None,
                  connection: sqlite3.Connection | None = None) -> FactSnapshot:
    """Return current and superseded quotations in a single read transaction."""
    empty = FactSnapshot(current=(), superseded=())
    with (nullcontext(connection) if connection is not None else closing(database.connect())) as con:
        if connection is None:
            con.execute('BEGIN')
        tables = {row['name'] for row in con.execute("""SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ('pending_utterances','call_fact_events')""")}
        if tables != {'pending_utterances', 'call_fact_events'}:
            return empty
        permission = con.execute("""SELECT l.generation,l.state FROM calls c LEFT JOIN call_location_consent l ON l.call_id=c.id
            WHERE c.id=? AND c.state='accepted' AND c.urgent=0 AND c.ended_at IS NULL
            """, (call_id,)).fetchone()
        if permission is None:
            return empty
        boundary = 2**63 - 1
        if through_utterance_id is not None:
            source = con.execute('SELECT seq FROM pending_utterances WHERE call_id=? AND request_id=?',
                                 (call_id, through_utterance_id)).fetchone()
            if source is None:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_subject_boundaries'").fetchone()
                changed = exists and con.execute('''SELECT 1 FROM call_fact_subject_boundaries
                    WHERE call_id=? LIMIT 1''', (call_id,)).fetchone()
                if changed:
                    rows = con.execute('''SELECT request_id FROM turns
                        WHERE session_id=? AND request_id<>?''', (call_id, through_utterance_id)).fetchall()
                    return FactSnapshot(current=(), superseded=(),
                        subject_excluded_utterance_ids=tuple(str(row['request_id']) for row in rows),
                        patient_subject_changed=True)
                return empty
            boundary = int(source['seq'])
        generation = int(permission['generation']) if permission['state'] == 'accepted' else 0
        subject_sequence = 0
        excluded: tuple[str, ...] = ()
        subject_changed = False
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_subject_boundaries'").fetchone():
            subject = con.execute('''SELECT source_sequence,utterance_id,created_at FROM call_fact_subject_boundaries
                WHERE call_id=? AND source_sequence<=? ORDER BY source_sequence DESC LIMIT 1''',
                (call_id, boundary)).fetchone()
            if subject is not None:
                subject_changed = True
                subject_sequence = int(subject['source_sequence'])
                rows = con.execute('''SELECT request_id FROM pending_utterances WHERE call_id=? AND seq<?
                    UNION SELECT request_id FROM turns WHERE session_id=? AND created_at<=? AND request_id<>?''',
                    (call_id, subject_sequence, call_id, subject['created_at'], subject['utterance_id'])).fetchall()
                excluded = tuple(str(row['request_id']) for row in rows)
        condition = '''EXISTS (SELECT 1 FROM call_fact_events newer
                WHERE newer.call_id=f.call_id AND newer.fact_key=f.fact_key
                AND newer.location_generation IN (0,?) AND newer.operation='replace'
                AND (newer.source_sequence>f.source_sequence OR
                    (newer.source_sequence=f.source_sequence AND newer.id>f.id))
                AND newer.source_sequence<=?)'''
        has_tombstones = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_fact_tombstones'").fetchone()
        if has_tombstones:
            condition += ''' OR EXISTS (SELECT 1 FROM call_fact_tombstones tombstone
                WHERE tombstone.call_id=f.call_id AND tombstone.fact_key=f.fact_key
                AND (tombstone.source_sequence>f.source_sequence OR
                    (tombstone.source_sequence=f.source_sequence AND tombstone.source_event_id>f.id))
                AND tombstone.source_sequence<=?)'''
        base = '''SELECT f.fact_key,f.quote,f.utterance_id FROM call_fact_events f
            WHERE f.call_id=? AND f.location_generation IN (0,?) AND f.source_sequence<=? AND f.source_sequence>=?'''
        params = (call_id, generation, boundary, subject_sequence, generation, boundary)
        if has_tombstones:
            params += (boundary,)
        order = ' ORDER BY f.source_sequence DESC,f.id DESC'
        rows = con.execute(base + ' AND NOT (' + condition + ')' + order + ' LIMIT 20', params).fetchall()
        superseded_rows = con.execute(base + ' AND (' + condition + ')' + order, params).fetchall()
        def evidence(rows: list) -> tuple[FactEvidence, ...]:
            return tuple(FactEvidence(key=row['fact_key'], quote=row['quote'], utterance_id=row['utterance_id'])
                         for row in reversed(rows))
        return FactSnapshot(current=evidence(rows), superseded=evidence(superseded_rows),
                            subject_excluded_utterance_ids=excluded, patient_subject_changed=subject_changed)
