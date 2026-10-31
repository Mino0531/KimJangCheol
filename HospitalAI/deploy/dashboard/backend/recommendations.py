"""Authenticated receipt of location-free hospital recommendation events."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field as InputField, model_validator
from sqlalchemy import Column, JSON, UniqueConstraint
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select

from backend.consultations import require_dashboard_token


class InvalidRecommendation(ValueError):
    """A recommendation field violates the receiver contract."""


class RecommendationRecord(SQLModel, table=True):
    __tablename__ = 'recommendation_results'
    __table_args__ = (UniqueConstraint('source', 'event_id'),)
    id: str = Field(primary_key=True)
    source: str
    event_id: str
    occurred_at: str = Field(index=True)
    data: dict = Field(sa_column=Column(JSON, nullable=False))


class HospitalSummary(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    name: str = InputField(min_length=1, max_length=200)
    phone: str | None = InputField(default=None, max_length=50)
    distance_km: float = InputField(ge=0, le=200, allow_inf_nan=False)
    specialty_match: Literal['yes', 'no', 'unknown'] = 'unknown'
    specialist_count: int | None = InputField(default=None, ge=0, le=10000)
    registered_schedule_status: Literal['within_hours', 'outside_hours', 'unknown']
    hours: dict[str, tuple[str, str]] | None = None
    source: str | None = InputField(default=None, max_length=200)

    @model_validator(mode='after')
    def validate_hours(self) -> 'HospitalSummary':
        days = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}
        if self.hours is not None:
            if not self.hours.keys() <= days:
                raise InvalidRecommendation('Invalid weekday')
            for start, end in self.hours.values():
                if any(len(value) != 4 or not value.isascii() or not value.isdigit() for value in (start, end)):
                    raise InvalidRecommendation('Invalid registered hours')
                start_hour, start_minute = int(start[:2]), int(start[2:])
                end_hour, end_minute = int(end[:2]), int(end[2:])
                if start_hour > 23 or start_minute > 59 or end_hour > 24 or end_minute > 59:
                    raise InvalidRecommendation('Invalid registered hours')
                if end_hour == 24 and end_minute != 0:
                    raise InvalidRecommendation('Invalid registered hours')
        return self


class RecommendationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    source: Literal['hospital-ai']
    event_id: str = InputField(min_length=1, max_length=128)
    call_id: str = InputField(min_length=1, max_length=128)
    occurred_at: datetime
    specialty: str | None = InputField(default=None, min_length=1, max_length=100)
    hospitals: list[HospitalSummary] = InputField(max_length=50)
    warning: str | None = InputField(default=None, max_length=4000)

    @model_validator(mode='after')
    def timezone_required(self) -> 'RecommendationInput':
        if self.occurred_at.utcoffset() is None:
            raise InvalidRecommendation('occurred_at must include a timezone')
        return self


def register_recommendation_routes(app: FastAPI, engine: Engine) -> None:
    @app.post('/api/team/recommendation-results', dependencies=[Depends(require_dashboard_token)])
    def receive_recommendation(body: RecommendationInput) -> JSONResponse:
        payload = body.model_dump(mode='json')
        with Session(engine) as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            previous = session.exec(select(RecommendationRecord).where(
                RecommendationRecord.source == body.source, RecommendationRecord.event_id == body.event_id)).first()
            if previous:
                if previous.data != payload:
                    raise HTTPException(409, 'Event ID already used with different content')
                return JSONResponse({'applied': False, 'duplicate': True, 'event_id': body.event_id,
                                     'call_id': body.call_id}, status_code=200)
            session.add(RecommendationRecord(id=str(uuid4()), source=body.source, event_id=body.event_id,
                                             occurred_at=body.occurred_at.astimezone(timezone.utc).isoformat(), data=payload))
            session.commit()
        return JSONResponse({'applied': True, 'duplicate': False, 'event_id': body.event_id,
                             'call_id': body.call_id}, status_code=201)

    @app.get('/api/team/recommendation-results', dependencies=[Depends(require_dashboard_token)])
    def list_recommendations(response: Response, limit: int = Query(100, ge=1, le=200),
                             offset: int = Query(0, ge=0)) -> list[dict]:
        response.headers['Cache-Control'] = 'no-store'
        with Session(engine) as session:
            rows = session.exec(select(RecommendationRecord).order_by(
                RecommendationRecord.occurred_at.desc(), RecommendationRecord.id).offset(offset).limit(limit)).all()
            return [row.data for row in rows]
