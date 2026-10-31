"""Local-only guidance when location consent has been withdrawn.

No caller text is returned in a response: the caller of these functions must
also keep it out of external LLM, retrieval and speech synthesis requests.
"""

import re
from typing import Final, Literal

from app.conversation_controls import end_requires_clarification
from app.emergency import emergency_detected

PrivateIntent = Literal['urgent', 'end', 'revoke_consent', 'revoke_location', 'accept_location', 'consultation']

_END: Final = re.compile(
    r'(?:(?:네|예|응|어|그럼|이제|저는|저))*(?:(?:전화|통화|상담)(?:를|을)?)?'
    r'(?:끊을게요|끊겠습니다|끊어요|마칠게요|마치겠습니다|끝낼게요|끝내겠습니다|'
    r'그만할게요|그만하겠습니다|종료할게요|종료하겠습니다)(?:안녕히계세요)?'
)
_WITHDRAW: Final = re.compile(
    r'(?:(?:네|예|응|어|그럼|이제|지금|바로|즉시|저는|저))*'
    r'(?:(?:(?:개인정보|정보)(?:수집|처리|제공)?)?(?:동의|동의한것|동의한거)|'
    r'(?:개인정보|정보)(?:수집|처리|제공))'
    r'(?:를|을|은|는|만)?(?:(?:지금|이제|바로|즉시))*'
    r'(?:철회|취소)(?:합니다|하겠습니다|할게요|해요|해|해주세요|해줘|할래요)?'
)
_LOCATION_WITHDRAW: Final = re.compile(
    r'(?:(?:네|예|응|어|그럼|이제|지금|바로|즉시))*(?:내|제)?'
    r'(?:위치정보|위치)(?:를|을|의|은|는)?(?:(?:지금|이제|바로|즉시))*'
    r'(?:(?:사용|이용)(?:동의)?|동의)?(?:를|을|은|는|만)?(?:(?:지금|이제|바로|즉시))*'
    r'(?:철회|취소)(?:합니다|하겠습니다|할게요|해요|해|해주세요|해줘|할래요)?'
)
_LOCATION_ACCEPT: Final = re.compile(
    r'(?:(?:네|예|응|어|그럼|이제))*(?:내|제)?(?:위치정보|위치)(?:를|을|의)?(?:다시)?'
    r'(?:(?:(?:사용|이용)(?:에|을|하는데|하는것에)?(?:다시)?)?'
    r'(?:동의합니다|동의하겠습니다|동의할게요|동의해요|동의해|동의|'
    r'허용합니다|허용할게요|허용해요|허용해주세요)'
    r'|(?:사용해도|이용해도|써도)(?:돼요|됩니다|좋아요|괜찮아요|괜찮습니다)'
    r'|(?:사용|이용)(?:할게요|하겠습니다))'
)
_FALLBACK: Final = (
    '위치 이용을 중단해 자세한 상담은 어려워요. '
    '병원에 가실 때 신분증과 드시는 약을 챙겨 주세요. 더 궁금한 점이 있으세요?'
)
_FALLBACKS: Final = (
    _FALLBACK,
    '병원에 가시기 전에 진료 시간을 전화로 확인해 보세요. 더 도와드릴까요?',
    '말씀은 듣고 있어요. 지금은 자세한 답변이 어려워요. 궁금한 점이 있으세요?',
)


def classify_private_intent(text: str, safety_history: tuple[str, ...] = ()) -> PrivateIntent:
    """Use conservative local controls and retain the call for everything else."""
    compact = ''.join(char for char in text if char.isalnum())
    direct = not re.search(r'''[?？"'“”‘’「」『』]''', text)
    if direct and _WITHDRAW.fullmatch(compact):
        return 'revoke_consent'
    if direct and _LOCATION_WITHDRAW.fullmatch(compact):
        return 'revoke_location'
    if emergency_detected(' '.join((*safety_history[-2:], text))):
        return 'urgent'
    if direct and _LOCATION_ACCEPT.fullmatch(compact):
        return 'accept_location'
    if direct and not end_requires_clarification(text) and _END.fullmatch(compact):
        return 'end'
    return 'consultation'


def private_fallback() -> str:
    """Return fixed, nonclinical visit-preparation guidance with no caller text."""
    return _FALLBACK


def private_fallback_for_turn(index: int) -> str:
    """Select among fixed prompts using a zero-based local consultation count."""
    return _FALLBACKS[max(index, 0) % len(_FALLBACKS)]
