"""Consent-gated lookup of public, approximate administrative-region coordinates."""

from __future__ import annotations

import json
import re
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.call_context import CallContext
from app.utterance_location import capture_location_revision, validate_source_generation


class RegionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str = Field(pattern=r"^[0-9]{10}$")
    province: str = Field(min_length=1, max_length=50)
    district: str = Field(min_length=1, max_length=80)
    locality: str = Field(min_length=2, max_length=80)
    latitude: float = Field(ge=30, le=40, allow_inf_nan=False)
    longitude: float = Field(ge=120, le=135, allow_inf_nan=False)


class RegionChoice(BaseModel):
    model_config = ConfigDict(frozen=True)
    province: str
    district: str
    locality: str


class RegionMatch(RegionChoice):
    code: str
    latitude: float
    longitude: float
    location_precision: Literal["region_reference"] = "region_reference"


class RegionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["matched", "ambiguous", "not_found"]
    match: RegionMatch | None = None
    candidates: tuple[RegionChoice, ...] = ()
    truncated: bool = False
    location_generation: int | None = None
    location_revision: int | None = None


class RegionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=200)
    source_location_generation: int | None = Field(default=None, ge=1, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)


def _compact(text: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", text).casefold()


def _province(record: RegionRecord) -> str:
    # The source workbook groups both regions into one label and code prefix.
    # Its five metropolitan districts identify the 광주 side of the grouping.
    if record.province == "전남광주통합특별시":
        if record.district in {"동구", "서구", "남구", "북구", "광산구"}:
            return "광주광역시"
        return "전라남도"
    return record.province


def _province_aliases(name: str) -> tuple[str, ...]:
    aliases = {
        "서울특별시": ("서울", "서울시"), "부산광역시": ("부산", "부산시"),
        "대구광역시": ("대구", "대구시"), "인천광역시": ("인천", "인천시"),
        "광주광역시": ("광주", "광주시"), "대전광역시": ("대전", "대전시"),
        "울산광역시": ("울산", "울산시"), "세종특별자치시": ("세종", "세종시"),
        "경기도": ("경기",), "강원특별자치도": ("강원", "강원도"),
        "충청북도": ("충북",), "충청남도": ("충남",),
        "전북특별자치도": ("전북", "전라북도"), "전라남도": ("전남",),
        "경상북도": ("경북",), "경상남도": ("경남",),
        "제주특별자치도": ("제주", "제주도"),
    }
    return (name, *aliases.get(name, ()))


def _district_aliases(name: str) -> tuple[str, ...]:
    # A one-character district stem such as 중구 -> 중 is too ambiguous.
    stem = name[:-1] if name.endswith(("시", "군", "구")) else name
    return (name, stem) if len(stem) >= 2 else (name,)


class RegionIndex:
    """In-memory index over public region reference points, not user locations."""

    def __init__(self, records: tuple[RegionRecord, ...]) -> None:
        self.records = records
        self.provinces = frozenset(_province(record) for record in records)
        self.districts = frozenset(record.district for record in records)

    def resolve(self, text: str) -> RegionResult:
        query = _compact(text)
        if not query:
            return RegionResult(status="not_found")
        matched = [record for record in self.records if _compact(record.locality) in query]
        if not matched:
            return RegionResult(status="not_found")
        province_tokens = {_compact(alias) for name in self.provinces for alias in _province_aliases(name)
                           if _compact(alias) in query}
        provinces = {name for name in self.provinces if any(_compact(alias) in province_tokens for alias in _province_aliases(name))}
        # 광주 also names a 경기도 city. A matching province token must not
        # silently impose that unrelated city as an extra district constraint.
        districts = {name for name in self.districts if any(_compact(alias) in query and _compact(alias) not in province_tokens
                                                           for alias in _district_aliases(name))}
        if provinces:
            matched = [record for record in matched if _province(record) in provinces]
        if districts:
            matched = [record for record in matched if record.district in districts]
        if not matched:
            return RegionResult(status="not_found")
        # Filter by the stated region first; concatenating the query may create
        # an unrelated longer locality across a district/locality boundary.
        longest = max(len(_compact(record.locality)) for record in matched)
        matched = [record for record in matched if len(_compact(record.locality)) == longest]
        if len(matched) == 1:
            record = matched[0]
            return RegionResult(status="matched", match=RegionMatch(
                code=record.code, province=_province(record), district=record.district,
                locality=record.locality, latitude=record.latitude, longitude=record.longitude))
        choices = sorted({(_province(record), record.district, record.locality) for record in matched})
        return RegionResult(status="ambiguous", candidates=tuple(
            RegionChoice(province=province, district=district, locality=locality)
            for province, district, locality in choices[:10]), truncated=len(choices) > 10)


@lru_cache(maxsize=1)
def public_region_index() -> RegionIndex:
    path = Path(__file__).resolve().parent.parent / "data" / "region_coordinates.json"
    with path.open(encoding="utf-8") as source:
        raw = json.load(source)
    return RegionIndex(tuple(RegionRecord.model_validate(row) for row in raw))


def region_router(context: CallContext, region_index: RegionIndex | None = None) -> APIRouter:
    """Resolve only while call and location consent remain current."""
    router = APIRouter(dependencies=[Depends(context.auth)])

    def generation(call_id: str, source_generation: int | None,
                   source_revision: int | None) -> tuple[int, bool, int]:
        with closing(context.database.connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute("""SELECT c.state,c.ended_at,c.urgent,l.state AS location_state,l.generation,
                EXISTS(SELECT 1 FROM location_consent_events e WHERE e.call_id=c.id AND e.state='revoked') AS revoked_before
                FROM calls c LEFT JOIN call_location_consent l ON l.call_id=c.id WHERE c.id=?""", (call_id,)).fetchone()
            valid = validate_source_generation(connection, call_id, source_generation, source_revision) if row else False
            revision = capture_location_revision(connection, call_id) if row else 0
        if row is None:
            raise HTTPException(404, "Call not found")
        if row["state"] != "accepted" or row["location_state"] != "accepted":
            raise HTTPException(403, "Call and location consent are required")
        if row["ended_at"] is not None or row["urgent"]:
            raise HTTPException(409, "Region search is unavailable for this call")
        if not valid:
            raise HTTPException(409, "Location consent changed; request a new search")
        return int(row["generation"]), bool(row["revoked_before"]), revision

    @router.post("/calls/{call_id}/regions/resolve", response_model=RegionResult)
    def resolve(call_id: str, body: RegionInput, response: Response) -> RegionResult:
        initial_generation, revoked_before, revision = generation(call_id, body.source_location_generation,
                                                                   body.source_location_revision)
        if body.source_location_generation is None:
            if initial_generation != 1 or revoked_before:
                raise HTTPException(409, "Location generation is required")
        elif body.source_location_generation != initial_generation:
            raise HTTPException(409, "Location consent changed; request a new search")
        try:
            index = region_index if region_index is not None else public_region_index()
            result = index.resolve(body.text)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(503, "Region data unavailable") from exc
        if generation(call_id, body.source_location_generation, body.source_location_revision) != (initial_generation, revoked_before, revision):
            raise HTTPException(409, "Location consent changed; request a new search")
        response.headers["Cache-Control"] = "no-store"
        return result.model_copy(update={"location_generation": initial_generation, "location_revision": revision})

    return router
