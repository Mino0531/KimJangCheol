"""Strict hospital recommendation payload, excluding coordinates and audio."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


def valid_time(value: str, *, end: bool) -> bool:
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        return False
    hour, minute = int(value[:2]), int(value[2:])
    return (hour < 24 and minute < 60) or (end and hour == 24 and minute == 0)


class HospitalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    distance_km: float = Field(ge=0, le=200, allow_inf_nan=False)
    specialist_count: int | None = Field(default=None, ge=0, le=10000)
    registered_schedule_status: Literal["within_hours", "outside_hours", "unknown"]
    hours: dict[str, tuple[str, str]] | None = None
    specialty_match: Literal["yes", "no", "unknown"]
    source: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid_registered_hours(self) -> HospitalPayload:
        if self.hours is not None:
            if not self.hours.keys() <= {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
                raise PydanticCustomError("registered_hours", "Invalid registered hours")
            for pair in self.hours.values():
                if not valid_time(pair[0], end=False) or not valid_time(pair[1], end=True):
                    raise PydanticCustomError("registered_hours", "Invalid registered hours")
        return self


class RecommendationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["hospital-ai"]
    event_id: str
    call_id: str
    occurred_at: AwareDatetime
    specialty: str | None = Field(default=None, max_length=100)
    hospitals: list[HospitalPayload] = Field(max_length=50)
    warning: str | None = Field(default=None, max_length=4000)
