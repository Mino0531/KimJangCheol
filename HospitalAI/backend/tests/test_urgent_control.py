"""An urgent model classification cannot contradict a clear hold/question."""

from dataclasses import dataclass

import anyio
import pytest

from app.urgent_intent import classify_urgent


@dataclass(frozen=True, slots=True)
class Config:
    low_model: str = 'test-low'
    allow_medical_data: bool = True


class MistakenEndModel:
    config = Config()

    async def _complete(self, model: str, system: str, user: str) -> str:
        return '{"intent":"end"}'


@pytest.mark.parametrize('text', [
    '그러면 여기서 끊어도 돼요?',
    '아니 아직 끊지 말고 잠깐만 기다려요',
    '끊으라는 말이야?',
])
def test_urgent_hangup_does_not_override_continuation_or_question(text: str) -> None:
    async def scenario() -> None:
        assert await classify_urgent(MistakenEndModel(), text, []) == 'continue'
    anyio.run(scenario)


def test_urgent_explicit_end_remains_available() -> None:
    async def scenario() -> None:
        assert await classify_urgent(MistakenEndModel(), '이제 끊을게요', []) == 'end'
    anyio.run(scenario)
