"""Bounded public-data lookup and transparent recommendation results."""

from __future__ import annotations

import base64
import re
from typing import Final, Literal

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.recommendation import HospitalCandidate, RecommendationQuery, RecommendationResult, recommend, straight_line_km
from app.recommend_hira import HiraClient, HiraDetails, HiraError, HiraHospital, HiraTooBroad
from app.orchestrator import SpeechOutput
from app.services import ServiceError


# HIRA subject codes. A missing mapping falls back to the unfiltered search.
SPECIALTY_CODES: Final = {
    "내과": "01", "신경과": "02", "정신건강의학과": "03", "외과": "04", "정형외과": "05",
    "신경외과": "06", "심장혈관흉부외과": "07", "성형외과": "08", "마취통증의학과": "09",
    "산부인과": "10", "소아청소년과": "11", "안과": "12", "이비인후과": "13",
    "피부과": "14", "비뇨의학과": "15", "영상의학과": "16", "방사선종양학과": "17",
    "병리과": "18", "진단검사의학과": "19", "재활의학과": "21", "핵의학과": "22",
    "가정의학과": "23", "응급의학과": "24", "직업환경의학과": "25", "예방의학과": "26",
    "치과": "49",
}


class LocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    location_consent: StrictBool
    specialty: str | None = Field(default=None, min_length=1, max_length=100)
    urgent: StrictBool = False
    include_audio: StrictBool = False


class RecommendationResponse(RecommendationResult):
    audio_base64: str | None = None
    audio_mime_type: str | None = None
    speech_text: str | None = None
    audio_status: Literal["not_requested", "synthesized", "unavailable"] = "not_requested"
    specialty: str | None = None
    specialty_source: Literal["chosen", "suggested", "unknown"] = "unknown"
    specialty_notice: str | None = None
    dashboard_delivery: Literal["pending", "sent", "disabled", "failed", "not_applicable"] = "not_applicable"
    dashboard_event_id: str | None = None


async def add_audio(result: RecommendationResult, tts: SpeechOutput, timeout: float, *,
                    specialty_requested: bool = False) -> RecommendationResponse:
    """Synthesize only retrieved public fields; never patient text or coordinates."""
    if result.status != "ok" or not result.hospitals:
        return RecommendationResponse(**result.model_dump(), audio_status="unavailable")
    hospital = result.hospitals[0]
    name = re.sub(r"[^가-힣A-Za-z0-9 ()-]", "", hospital.name)[:80].strip()
    phone = re.sub(r"[^0-9+ -]", "", hospital.phone or "")[:30].strip()
    message = f"{name or '가까운 병원'}부터 알아보시면 좋겠어요."
    if not specialty_requested:
        message = "진료과를 확인하지 못해 가까운 의료기관을 알려드릴게요. " + message
    if specialty_requested:
        if hospital.specialty_match == "no":
            message += " 추천 진료과는 등록 정보에 없어요."
        if hospital.specialty_match == "unknown" and hospital.specialist_count is None:
            message += " 추천 진료과와 전문의 정보는 확인하지 못했어요."
        elif hospital.specialty_match == "unknown":
            message += " 추천 진료과는 확인하지 못했어요."
        elif hospital.specialist_count is None:
            message += " 전문의 정보는 확인하지 못했어요."
    if phone:
        message += f" 전화번호는 {phone}입니다."
    message += " 가시기 전에 진료하는지 전화로 확인해 주세요."
    try:
        with anyio.fail_after(min(timeout, 20)):
            voice = await tts.deliver(message, "recommendation")
        if voice.status != "synthesized" or not voice.audio or voice.mime_type != "audio/mpeg" or len(voice.audio) > 5_000_000:
            return RecommendationResponse(**result.model_dump(), audio_status="unavailable")
        return RecommendationResponse(**result.model_dump(), audio_status="synthesized",
                                      audio_mime_type=voice.mime_type,
                                      audio_base64=base64.b64encode(voice.audio).decode("ascii"), speech_text=message)
    except (TimeoutError, ServiceError, httpx2.HTTPError, OSError, RuntimeError):
        return RecommendationResponse(**result.model_dump(), audio_status="unavailable")


async def _nearby(client: HiraClient, query: RecommendationQuery, *, specialty_code: str | None = None) -> tuple[list[HiraHospital], int]:
    """Bound the result set while adapting to urban and rural density."""
    radii = (500, 1000, 2000, 5000, 10000, 20000, 30000)
    index = 2
    previous: tuple[list[HiraHospital], int] | None = None
    while True:
        try:
            if specialty_code is None:
                found = await client.list_nearby(query.latitude, query.longitude, radii[index])
            else:
                found = await client.list_nearby(query.latitude, query.longitude, radii[index], specialty_code=specialty_code)
        except HiraTooBroad:
            if previous is not None and previous[0]:
                return previous
            if index == 0 or previous is not None:
                raise
            index -= 1
            continue
        if (found and specialty_code is not None) or len(found) >= 10 or index == len(radii) - 1:
            return found, radii[index]
        previous = found, radii[index]
        index += 1


async def public_recommendations(query: RecommendationQuery, service_key: str) -> RecommendationResult:
    """Return only source-backed candidates; failures stay explicit and unlogged."""
    if query.urgent:
        return recommend(query, [])
    if not service_key:
        return RecommendationResult(status="unavailable", warning="공공데이터 API 연결이 설정되지 않았습니다.")
    try:
        with anyio.fail_after(65):
            async with HiraClient(service_key) as client:
                specialty_code = SPECIALTY_CODES.get(query.specialty) if query.specialty else None
                nearby, radius = await _nearby(client, query, specialty_code=specialty_code)
                filtered_empty = specialty_code is not None and not nearby
                if filtered_empty:
                    nearby, radius = await _nearby(client, query)
                bases = [HospitalCandidate(id=item.id, name=item.name, address=item.address or None,
                                           phone=item.phone or None, latitude=item.latitude,
                                           longitude=item.longitude, source="건강보험심사평가원") for item in nearby]
                nearest = sorted(bases, key=lambda item: (straight_line_km(query, item), item.id))[:30]
                details: dict[str, HiraDetails] = {}
                limiter = anyio.CapacityLimiter(8)

                async def load(candidate: HospitalCandidate) -> None:
                    async with limiter:
                        try:
                            with anyio.fail_after(20):
                                details[candidate.id] = await client.details(candidate.id)
                        except (HiraError, TimeoutError):
                            details[candidate.id] = HiraDetails(unavailable=("specialties", "specialists", "hours"))

                async with anyio.create_task_group() as tasks:
                    for candidate in nearest:
                        tasks.start_soon(load, candidate)
            enriched = [candidate.model_copy(update={
                "specialties": details[candidate.id].specialties,
                "specialists": details[candidate.id].specialists,
                "hours": details[candidate.id].hours,
                "open_status": details[candidate.id].open_status,
                "updated_at": details[candidate.id].updated_at,
            }) for candidate in nearest]
            result = recommend(query, enriched)
            warning = f"반경 {radius / 1000:g}km 내 좌표가 확인된 후보 중 가장 가까운 최대 30곳만 평가합니다. " + (result.warning or "")
            if filtered_empty:
                warning += " 요청 진료과를 최대 30km까지 찾지 못해 다른 주변 기관을 확인합니다."
            if any(detail.unavailable for detail in details.values()):
                warning += " 일부 상세정보를 확인하지 못했습니다."
            return result.model_copy(update={"warning": warning, "search_radius_km": radius / 1000,
                                             "considered_count": len(nearest), "total_in_radius": None})
    except HiraTooBroad:
        return RecommendationResult(status="unavailable", warning="검색 범위의 의료기관이 너무 많아 목록 조회 제한을 초과했습니다. 위치를 조정해 다시 시도하세요.")
    except (HiraError, TimeoutError):
        return RecommendationResult(status="unavailable", warning="공공데이터를 조회하지 못했습니다. 잠시 후 다시 시도하세요.")
