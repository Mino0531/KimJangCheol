"""Normalize generated answers before displaying or synthesizing speech."""

import json
import re
from dataclasses import dataclass

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError


class ResponseFormatError(Exception):
    """A structured model response cannot safely be shown to the caller."""


class _StructuredAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    display: str | None = Field(default=None, validation_alias=AliasChoices(
        "display_text", "displayText", "display", "표시", "표시할 내용", "화면 표시"))
    speech: str | None = Field(default=None, validation_alias=AliasChoices(
        "tts_text", "ttsText", "speech_text", "speech", "tts", "TTS", "음성", "TTS출력할 내용"))
    citations: list[str] = Field(default_factory=list, validation_alias=AliasChoices("citations", "references"))


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    display: str
    speech: str
    citations: tuple[str, ...] = ()


def _patient_answer(display: str, speech: str, citations: tuple[str, ...] = ()) -> GeneratedAnswer:
    pattern = r"\[((?:patient|clinic):[^\]]+)\]"
    references = tuple(re.findall(pattern, display + "\n" + speech)) + citations
    visible = re.sub(pattern, "", display).strip()
    spoken = re.sub(pattern, "", speech).strip()
    if not visible or not spoken:
        raise ResponseFormatError("Answer lacks patient-facing text")
    return GeneratedAnswer(visible, spoken, references)


def _looks_structured(value: str) -> bool:
    return (value.startswith(("{", "```")) or bool(re.match(r'^\[\s*[{\"]', value))
            or bool(re.search(r'\{\s*"[^"\\]{1,80}"\s*:', value)))


def parse_generated_answer(value: str) -> GeneratedAnswer:
    """Only patient-facing fields may cross the model-output boundary."""
    raw = value.strip()
    if not raw:
        raise ResponseFormatError("Answer is empty")
    if raw.startswith('"'):
        nested = raw
        for _ in range(4):
            try:
                decoded = json.loads(nested)
            except json.JSONDecodeError as exc:
                if nested != raw or raw.startswith(('"{', '"\\"')):
                    raise ResponseFormatError("Invalid encoded answer") from exc
                break
            if not isinstance(decoded, str):
                break
            decoded = decoded.strip()
            if _looks_structured(decoded):
                raw = decoded
                break
            if not decoded.startswith('"'):
                break
            nested = decoded
        else:
            raise ResponseFormatError("Encoded answer exceeds nesting limit")
    if raw.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if match is None:
            raise ResponseFormatError("Answer contains an incomplete code block")
        raw = match.group(1).strip()
        if not raw.startswith("{"):
            raise ResponseFormatError("Code block does not contain an answer object")
    embedded = re.search(r'\{\s*"[^"\\]{1,80}"\s*:', raw)
    if embedded is not None and not raw.startswith("{"):
        raw = raw[embedded.start():]
    if raw.startswith("{") or re.match(r'^\[\s*[{\"]', raw):
        try:
            parsed = _StructuredAnswer.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ResponseFormatError("Invalid structured answer") from exc
        display = (parsed.display or parsed.speech or "").strip()
        speech = (parsed.speech or parsed.display or "").strip()
        if not display or not speech or _looks_structured(display) or _looks_structured(speech):
            raise ResponseFormatError("Structured answer lacks safe text fields")
        return _patient_answer(display, speech, tuple(parsed.citations))
    return _patient_answer(raw, raw)
