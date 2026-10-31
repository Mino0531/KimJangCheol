"""Validated text consent for the telephone prototype."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ConsentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=200)


class LocationConsentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    accepted: StrictBool
    source_location_generation: int | None = Field(default=None, ge=0, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)


class UrgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str | None = Field(default=None, max_length=10000)
    source_location_generation: int | None = Field(default=None, ge=0, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)


def consent_decision(text: str) -> Literal["accepted", "declined", "ambiguous"]:
    if "?" in text or "？" in text:
        return "ambiguous"
    compact = re.sub(r"[\s,.!?。]", "", text).lower()
    if compact in {"네", "예"} or re.fullmatch(
        r"(?:어+|음+|아+|네|예|응|그래요|그래)*"
        r"동의(?:|합니다|해요|해|할게요|하겠습니다|했어|했어요|했습니다)", compact
    ):
        return "accepted"
    if compact in {"동의하지않습니다", "동의안합니다", "동의안해요", "동의하지않아요", "거부", "거부합니다", "아니요", "아니오", "싫어요"}:
        return "declined"
    return "ambiguous"


def split_consent(text: str) -> tuple[Literal["accepted", "declined", "ambiguous"], str | None]:
    """Separate an explicit complete consent sentence from later caller speech.

    Question, quotation, cancellation, or partial consent language in the
    remainder is deliberately ambiguous; never release it for consultation.
    """
    boundary = re.search(r"[.。]", text)
    if boundary is None:
        return consent_decision(text), None
    first = text[:boundary.start()]
    remainder = text[boundary.end():]
    if not remainder.strip():
        return consent_decision(text), None
    decision = consent_decision(first)
    if decision == "declined":
        return "declined", None
    if decision != "accepted":
        return "ambiguous", None
    if re.search(
        r"동의|거부|거절|철회|취소|개인정보|위치|수집|제공|싫|아니|아냐|원치|원하지|"
        r"안\s*(?:할래|하겠|해요|합니다|했|돼|되)|라고|라는\s*(?:말|뜻)|말했|말하지",
        remainder,
    ):
        return "ambiguous", None
    return "accepted", remainder.strip()
