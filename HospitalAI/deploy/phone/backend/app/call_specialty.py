"""Select a tentative department without treating symptoms as a diagnosis."""

from dataclasses import dataclass
from typing import Literal

from app.db import Row
from app.specialty import suggest_specialty


_DENIAL_MARKERS = ("없", "않", "아니", "안아파", "안아프", "안나", "안해", "안차", "못")
_TOPICS: dict[str, tuple[str, ...]] = {
    "cough": ("기침",), "fever": ("열", "발열"), "breathing": ("숨", "호흡"),
    "phlegm": ("가래",), "nose": ("콧물",), "vomiting": ("구토", "토했"),
    "knee": ("무릎",), "back": ("허리",), "shoulder": ("어깨",),
    "ankle": ("발목",), "wrist": ("손목",),
    "dental": ("치통", "이가", "이빨", "어금니", "잇몸"),
}


def _topics(compact: str) -> set[str]:
    return {name for name, words in _TOPICS.items() if any(word in compact for word in words)}


@dataclass(frozen=True, slots=True)
class CallSpecialty:
    specialty: str | None
    source: Literal["chosen", "suggested", "unknown"]
    notice: str | None
    urgent: bool = False


def select_call_specialty(chosen: str | None, history: list[Row]) -> CallSpecialty:
    """Respect explicit choice and use only recent turns for a tentative hint."""
    if not history:
        return CallSpecialty(chosen, "chosen" if chosen else "unknown", None)
    turns = [str(row["input_text"])[-1000:] for row in history[-3:]]
    combined = " ".join(turns)
    combined_suggestion = suggest_specialty(combined)
    if combined_suggestion.status == "urgent":
        return CallSpecialty(None, "unknown", combined_suggestion.reason, urgent=True)
    if chosen:
        return CallSpecialty(chosen, "chosen", None)
    if any(term in combined for term in ("농약", "살충제", "제초제")):
        return CallSpecialty(None, "unknown", "농약 노출 여부와 증상을 확인해야 합니다.")

    hints: set[str] = set()
    positive_topics: set[str] = set()
    denied_topics: set[str] = set()
    for text in turns:
        compact = "".join(text.lower().split())
        topics = _topics(compact)
        if compact in {"네", "예", "응", "그래요", "맞아요", "맞습니다", "그렇습니다", "맞아", "네맞아요"}:
            return CallSpecialty(None, "unknown", "말씀하신 내용을 다시 확인해야 합니다.")
        if any(marker in compact for marker in _DENIAL_MARKERS):
            # An unqualified or contradictory denial cannot safely inherit an earlier symptom.
            if len(topics) != 1 or topics & positive_topics:
                return CallSpecialty(None, "unknown", "말씀하신 내용을 다시 확인해야 합니다.")
            denied_topics.update(topics)
            continue
        if topics & denied_topics:
            return CallSpecialty(None, "unknown", "말씀하신 내용을 다시 확인해야 합니다.")
        suggestion = suggest_specialty(text)
        if suggestion.status == "suggested" and suggestion.specialty is not None:
            hints.add(suggestion.specialty)
            positive_topics.update(topics)
        elif topics:
            # A new symptom that doesn't map clearly must not be discarded.
            return CallSpecialty(None, "unknown", "원하시는 진료과를 확인해야 합니다.")
    if len(hints) == 1:
        return CallSpecialty(next(iter(hints)), "suggested", "진료과 후보이며 의료진 확인이 필요합니다.")
    return CallSpecialty(None, "unknown", "원하시는 진료과를 확인해야 합니다.")
