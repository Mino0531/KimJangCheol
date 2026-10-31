"""Destructive end decisions need an affirmative current request."""

import pytest

from app.call_interpret import classify_intent
from test_call_interpret import FakeLow


@pytest.mark.anyio
@pytest.mark.parametrize("text", [
    "그러면 여기서 끊어도 돼요?",
    "여기서 끊어도 되나요",
    "아니 아직 끊지 말고 잠깐만 기다려요",
    "상담 그만하지 말아 주세요",
    "끝내지 마세요",
    "잠시만 기다려 주세요",
])
async def test_question_or_continuation_vetoes_model_end(text: str) -> None:
    provider = FakeLow('{"intent":"end"}')
    assert await classify_intent(provider, "caller", text, []) == "consultation"
    assert len(provider.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["이제 끊을게요", "상담 그만할게요", "안 기다려도 돼요. 이제 끊어요"])
async def test_explicit_end_remains_model_decision(text: str) -> None:
    provider = FakeLow('{"intent":"end"}')
    assert await classify_intent(provider, "caller", text, []) == "end"
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_veto_never_overrides_revocation() -> None:
    provider = FakeLow('{"intent":"revoke_consent"}')
    assert await classify_intent(provider, "caller", "끊지 말고 개인정보 동의만 취소할게요", []) == "revoke_consent"
