"""Consent-scoped public hospital information for follow-up conversation."""

import sqlite3
from contextlib import closing, nullcontext
from typing import Final, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db import Database
from app.recommend_payload import HospitalPayload, RecommendationPayload
from app.services import emergency_detected

MAX_RECOMMENDATION_BODY: Final = 24_000
_TABLES: Final = frozenset({"calls", "call_location_consent", "recommendation_outbox",
                          "municipal_recommendation_generations", "recommendation_cancellations",
                          "recommendation_failures"})


class RecommendationSource(TypedDict):
    id: str
    type: str
    text: str
    source: str


class RecommendationContext(BaseModel):
    """Public fields only; identities, coordinates, and patient data never cross."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(exclude=True, repr=False, min_length=1)
    location_generation: int = Field(exclude=True, repr=False, gt=0)
    specialty: str | None = Field(default=None, max_length=100)
    hospitals: tuple[HospitalPayload, ...] = Field(max_length=3)
    warning: str | None = Field(default=None, max_length=600)

    def as_source(self) -> RecommendationSource:
        """Ground hospital follow-up answers with the existing citation contract."""
        return {"id": "clinic:recent-recommendation", "type": "clinic",
                "text": self.model_dump_json(), "source": "이 통화에서 조회한 공공 의료기관 정보"}


def load_recommendation_context(database: Database, call_id: str, *,
                                connection: sqlite3.Connection | None = None) -> RecommendationContext | None:
    """Read the latest usable result for this call and current location consent."""
    with closing(database.connect()) if connection is None else nullcontext(connection) as con:
        tables = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _TABLES <= tables:
            return None
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(calls)")}
        active_clause = "AND c.ended_at IS NULL" if "ended_at" in columns else ""
        row = con.execute(f"""SELECT o.event_id,o.body,g.generation FROM recommendation_outbox o
            JOIN calls c ON c.id=o.call_id
            JOIN call_location_consent l ON l.call_id=c.id
            JOIN municipal_recommendation_generations g ON g.event_id=o.event_id
            WHERE o.call_id=? AND c.state='accepted' AND c.urgent=0 {active_clause}
            AND l.state='accepted' AND l.generation=g.generation
            AND NOT EXISTS (SELECT 1 FROM recommendation_cancellations x WHERE x.event_id=o.event_id)
            AND NOT EXISTS (SELECT 1 FROM recommendation_failures f WHERE f.event_id=o.event_id)
            AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=c.id AND t.status='emergency')
            ORDER BY o.rowid DESC LIMIT 1""", (call_id,)).fetchone()
        if row is None or len(str(row["body"])) > MAX_RECOMMENDATION_BODY:
            return None
        history = con.execute("SELECT input_text FROM turns WHERE session_id=?", (call_id,))
        if any(emergency_detected(str(turn["input_text"])) for turn in history):
            return None
    try:
        payload = RecommendationPayload.model_validate_json(str(row["body"]))
    except ValidationError:
        return None
    if payload.call_id != call_id or payload.event_id != str(row["event_id"]) or not payload.hospitals:
        return None
    return RecommendationContext(event_id=str(row["event_id"]), location_generation=int(row["generation"]),
                                 specialty=payload.specialty, hospitals=tuple(payload.hospitals[:3]),
                                 warning=payload.warning[:600] if payload.warning else None)
