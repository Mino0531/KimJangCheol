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


class UrgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str | None = Field(default=None, max_length=10000)


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
