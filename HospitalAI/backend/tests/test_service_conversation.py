"""Social service conversation cannot weaken factual medical grounding."""

import json

import pytest

from app.services import INSUFFICIENT_MESSAGE, OpenAICompatibleLLM, OpenAIConfig
from app.call_interpret import classify_intent
from test_call_interpret import FakeLow


@pytest.mark.anyio
@pytest.mark.parametrize("sources", [[], [{"id": "clinic:1", "text": "준비물 참고"}]])
@pytest.mark.parametrize("message", [
    "네, 더 궁금한 점이 있으면 말씀해 주세요.",
    "네, 천천히 말씀해 주세요. 듣고 있을게요.",
    "병원 방문 준비를 돕는 AI 안내 서비스예요. 궁금한 점을 말씀해 주세요.",
    "통화를 마치시려면 끊겠다고 말씀해 주세요. 더 궁금한 점이 있으면 이어서 말씀해 주세요.",
])
async def test_exact_nonclinical_service_message_can_continue_without_citation(message: str, sources: list[dict[str, str]]) -> None:
    answer = json.dumps({"display_text": message, "tts_text": message, "citations": []}, ensure_ascii=False)

    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return answer

    provider = Provider(OpenAIConfig(base_url="https://example.test/v1", api_key="fake",
                                    reasoning_model="test", allow_medical_data=True))
    assert await provider.reasoning("네, 고마워요", sources) == answer


@pytest.mark.anyio
@pytest.mark.parametrize("sources", [[], [{"id": "clinic:1", "text": "준비물 참고"}]])
@pytest.mark.parametrize("display,speech,citations", [
    ("네, 더 궁금한 점이 있으면 말씀해 주세요. 약은 드시지 마세요.",
     "네, 더 궁금한 점이 있으면 말씀해 주세요. 약은 드시지 마세요.", []),
    ("네, 더 궁금한 점이 있으면 말씀해 주세요.", "괜찮으니 병원에 가지 않아도 돼요.", []),
    ("네, 더 궁금한 점이 있으면 말씀해 주세요.", "네, 더 궁금한 점이 있으면 말씀해 주세요.", ["clinic:forged"]),
])
async def test_added_medical_claim_or_forged_source_is_still_rejected(display: str, speech: str, citations: list[str], sources: list[dict[str, str]]) -> None:
    answer = json.dumps({"display_text": display, "tts_text": speech, "citations": citations}, ensure_ascii=False)

    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return answer

    provider = Provider(OpenAIConfig(base_url="https://example.test/v1", api_key="fake",
                                    reasoning_model="test", allow_medical_data=True))
    assert await provider.reasoning("질문", sources) == INSUFFICIENT_MESSAGE


@pytest.mark.anyio
async def test_service_acknowledgement_waits_without_another_search_or_model_call() -> None:
    provider = FakeLow('{"intent":"answer"}')
    assert await classify_intent(provider, "assistant", "네, 더 궁금한 점이 있으면 말씀해 주세요.", []) == "follow_up"
    assert provider.calls == []
