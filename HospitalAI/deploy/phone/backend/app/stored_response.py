"""Safe presentation of legacy answer strings without modifying stored history."""

from app.db import Row
from app.response_format import ResponseFormatError, parse_generated_answer
from app.services import INSUFFICIENT_MESSAGE


def display_text(raw: str) -> str:
    try:
        return parse_generated_answer(raw).display
    except ResponseFormatError:
        return INSUFFICIENT_MESSAGE


def stored_audio_safe(raw: str) -> bool:
    """Old synthesized audio is safe only if no formatting was stripped."""
    try:
        answer = parse_generated_answer(raw)
    except ResponseFormatError:
        return False
    return answer.display == raw.strip() and answer.speech == raw.strip()


def visible_turn(row: Row) -> Row:
    result = dict(row)
    for key in ("initial_text", "final_text"):
        raw = result.get(key)
        if isinstance(raw, str):
            result[key] = display_text(raw)
    return result
