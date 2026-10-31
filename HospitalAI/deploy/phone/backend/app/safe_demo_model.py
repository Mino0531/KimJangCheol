"""A safe placeholder when no live reasoning model is configured."""

from dataclasses import dataclass

from app.services import MockLLM, SplitLLM

DEMO_ANSWER = "지금은 확인이 어렵습니다. 의료기관에 문의해 주세요."


@dataclass(frozen=True, slots=True)
class SafeDemoModel:
    """Never repeat unreviewed source text as medical advice in mock mode."""
    original: MockLLM | SplitLLM

    async def low(self, text: str) -> str:
        return await self.original.low(text)

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        return DEMO_ANSWER
