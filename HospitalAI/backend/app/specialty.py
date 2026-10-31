"""Conservative, non-diagnostic symptom-to-department hints for local search.

These small lexical rules do not assess clinical urgency or replace medical review.
"""

from dataclasses import dataclass
from typing import Final, Literal

from app.services import emergency_detected
from app.fall_guidance import has_leg_fall_pain


@dataclass(frozen=True, slots=True)
class SpecialtySuggestion:
    specialty: str | None
    status: Literal["suggested", "unknown", "urgent"]
    reason: str


_PESTICIDE_TERMS: Final = ("농약", "살충제", "제초제")
_NEGATION: Final = ("없", "않", "아니", "아닌", "안아프", "안나", "안해", "못느")
_RESPIRATORY: Final = ("기침", "발열", "열이나", "열나요", "열납니다")
_ORTHOPEDIC: Final = ("무릎", "허리", "어깨", "발목", "손목")
_PAIN: Final = ("아파", "아프", "아픕", "통증", "쑤셔", "쑤시", "욱신", "시큰", "저려")
_DENTAL: Final = ("치통", "이가아파", "이가아프", "이빨", "어금니", "잇몸")


def suggest_specialty(text: str) -> SpecialtySuggestion:
    """Return a tentative department only for a single, clear symptom pattern."""
    compact = "".join(text.lower().split())
    if emergency_detected(text):
        return SpecialtySuggestion(None, "urgent", "먼저 응급 여부를 확인해야 합니다.")
    if any(term in compact for term in _PESTICIDE_TERMS):
        return SpecialtySuggestion(None, "unknown", "농약 노출 여부와 증상을 확인해야 합니다.")
    if any(term in compact for term in _NEGATION):
        return SpecialtySuggestion(None, "unknown", "말씀하신 내용을 다시 확인해야 합니다.")
    hints: set[str] = set()
    if any(term in compact for term in _RESPIRATORY):
        hints.add("내과")
    if (any(term in compact for term in _ORTHOPEDIC) and any(term in compact for term in _PAIN)) or has_leg_fall_pain(text):
        hints.add("정형외과")
    if any(term in compact for term in _DENTAL) and any(term in compact for term in _PAIN + ("치통",)):
        hints.add("치과")
    if len(hints) != 1:
        return SpecialtySuggestion(None, "unknown", "원하시는 진료과를 확인해야 합니다.")
    return SpecialtySuggestion(hints.pop(), "suggested", "진료과 후보이며 의료진 확인이 필요합니다.")
