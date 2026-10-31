"""Deterministic, uncertainty-aware ranking of public facility records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class RecommendationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    specialty: str | None = Field(default=None, min_length=1, max_length=100)
    urgent: bool = False
    limit: int = Field(default=10, ge=1, le=50)
    at: AwareDatetime | None = None


class HospitalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    address: str | None = None
    phone: str | None = None
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    specialties: tuple[str, ...] | None = None
    specialists: dict[str, int] | None = None
    open_status: Literal["open", "closed", "unknown"] = "unknown"
    hours: dict[str, tuple[str, str]] | None = None
    source: str
    updated_at: str | None = None


class HospitalRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    address: str | None
    phone: str | None
    distance_km: float
    specialty_match: Literal["yes", "no", "unknown"]
    specialist_count: int | None
    open_status: Literal["open", "closed", "unknown"]
    registered_schedule_status: Literal["within_hours", "outside_hours", "unknown"]
    hours: dict[str, tuple[str, str]] | None
    source: str
    updated_at: str | None
    score: float
    known_factors: int
    total_factors: int
    reasons: tuple[str, ...]


class RecommendationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["ok", "urgent", "unavailable"]
    hospitals: tuple[HospitalRecommendation, ...] = ()
    warning: str | None = None
    search_radius_km: float | None = None
    considered_count: int | None = None
    total_in_radius: int | None = None


def straight_line_km(query: RecommendationQuery, hospital: HospitalCandidate) -> float:
    """Haversine great-circle distance in kilometres, not road distance."""
    lat1, lat2 = radians(query.latitude), radians(hospital.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(hospital.longitude - query.longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1.0, value)))


def _minutes(value: str, *, end: bool = False) -> int | None:
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        return None
    hour, minute = int(value[:2]), int(value[2:])
    if hour == 24 and minute == 0 and end:
        return 1440
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def registered_schedule_status(query: RecommendationQuery, hospital: HospitalCandidate) -> Literal["within_hours", "outside_hours", "unknown"]:
    """Weekly schedule only; holidays, breaks, and exceptional closures are unknown."""
    if not hospital.hours:
        return "unknown"
    korea = timezone(timedelta(hours=9), "KST")
    now = query.at.astimezone(korea) if query.at else datetime.now(korea)
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    today = hospital.hours.get(days[now.weekday()])
    prior = hospital.hours.get(days[(now.weekday() - 1) % 7])
    current = now.hour * 60 + now.minute
    if prior:
        prior_start, prior_end = _minutes(prior[0]), _minutes(prior[1], end=True)
        if prior_start is not None and prior_end is not None and prior_start > prior_end and current < prior_end:
            return "within_hours"
    if not today:
        return "unknown"
    start, end = _minutes(today[0]), _minutes(today[1], end=True)
    if start is None or end is None or start == end:
        return "unknown"
    if start < end:
        return "within_hours" if start <= current < end else "outside_hours"
    return "within_hours" if current >= start else "outside_hours"


def recommend(query: RecommendationQuery, candidates: list[HospitalCandidate]) -> RecommendationResult:
    """Rank verified attributes without turning missing fields into negative facts."""
    if query.urgent:
        return RecommendationResult(status="urgent", warning="응급 가능성이 있으면 즉시 119 또는 응급의료기관에 연락하세요.")
    output: list[tuple[float, HospitalRecommendation]] = []
    for candidate in candidates:
        distance = straight_line_km(query, candidate)
        match: Literal["yes", "no", "unknown"] = "unknown"
        count = None
        schedule = registered_schedule_status(query, candidate)
        if query.specialty and candidate.specialties is not None:
            match = "yes" if query.specialty in candidate.specialties else "no"
        if query.specialty and candidate.specialists is not None:
            count = candidate.specialists.get(query.specialty)
        known_factors = 1
        total_factors = 1
        reasons = [f"직선거리 {distance:.1f}km"]
        if query.specialty:
            total_factors += 1
            if match == "unknown":
                reasons.append("요청 진료과 미확인")
            else:
                known_factors += 1
                reasons.append("요청 진료과 확인" if match == "yes" else "요청 진료과 미등록")
            total_factors += 1
            if count is None:
                reasons.append("해당 과 전문의 수 미확인")
            else:
                known_factors += 1
                reasons.append(f"해당 과 등록 전문의 {count}명")
        total_factors += 1
        if schedule != "unknown":
            known_factors += 1
            reasons.append("등록 요일 진료시간 내" if schedule == "within_hours" else "등록 요일 진료시간 외")
        else:
            reasons.append("등록 요일 진료시간 미확인")
        # Compatibility field: reflects only the primary ordering, not quality or availability.
        closeness = 1 / (1 + distance / 10)
        if query.specialty:
            band = {"yes": 2, "unknown": 1, "no": 0}[match]
            score = (band + closeness) * (100 / 3)
        else:
            score = closeness * 100
        output.append((distance, HospitalRecommendation(
            id=candidate.id, name=candidate.name, address=candidate.address, phone=candidate.phone,
            distance_km=round(distance, 2), specialty_match=match, specialist_count=count,
            open_status=candidate.open_status, registered_schedule_status=schedule,
            hours=candidate.hours, source=candidate.source,
            updated_at=candidate.updated_at, score=round(score, 1),
            known_factors=known_factors, total_factors=total_factors, reasons=tuple(reasons),
        )))
    match_order = {"yes": 0, "unknown": 1, "no": 2}
    schedule_order = {"within_hours": 0, "unknown": 1, "outside_hours": 2}
    output.sort(key=lambda item: (
        match_order[item[1].specialty_match] if query.specialty else 0,
        item[0],
        0 if item[1].specialist_count is not None and item[1].specialist_count > 0
        else 1 if item[1].specialist_count is None else 2,
        schedule_order[item[1].registered_schedule_status],
        item[1].id,
    ))
    warning = ("등록 진료과 일치 여부와 직선거리 순으로 정렬합니다. 미확인은 등록되지 않았다는 뜻이 아니며, "
               "점수는 의료의 질이나 실제 진료 가능성의 확률이 아닙니다. "
               "직선거리와 등록 요일 시간 기준이며, 공휴일·임시휴진·점심시간과 전문의 근무 여부는 의료기관에 확인하세요.")
    return RecommendationResult(status="ok", hospitals=tuple(item for _, item in output[:query.limit]), warning=warning)
