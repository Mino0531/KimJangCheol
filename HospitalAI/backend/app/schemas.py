"""HTTP input validation."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

CallerContext = Literal["general", "hospital_search_offer", "visit_preparation_offer"]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    phone: str = Field(min_length=7, max_length=30)
    verification_token: str | None = Field(default=None, max_length=256)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    text: str = Field(min_length=1, max_length=10000)
    caller_context: CallerContext = "general"
    source_location_generation: int | None = Field(default=None, ge=0, strict=True)
    source_location_revision: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("text")
    @classmethod
    def reject_blank(cls, text: str) -> str:
        if not text.strip():
            raise PydanticCustomError("blank_speech", "Speech text cannot be blank")
        return text


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    turn_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=10000)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=128)
