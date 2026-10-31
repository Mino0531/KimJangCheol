"""Structured answer boundaries must never expose transport JSON to callers."""

import json
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle, process_turn
from app.services import INSUFFICIENT_MESSAGE, WAIT_MESSAGE, OpenAICompatibleLLM, OpenAIConfig, TTSResult


class AnswerModel:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def low(self, text: str) -> str:
        return WAIT_MESSAGE

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        return self.answer


class RecordSpeech:
    def __init__(self) -> None:
        self.final = ""

    async def deliver(self, text: str, kind: str) -> TTSResult:
        if kind == "final":
            self.final = text
        return TTSResult("simulated", kind, text, False)


def run_turn(tmp_path: Path, answer: str) -> tuple[str, str]:
    db = Database(tmp_path / "answer.sqlite")
    db.initialize(seed=True)
    session = db.create_session("+821000000001", "sample-patient-1")
    turn, _ = db.create_turn(str(session["id"]), "id1", "기침이 있어요")
    speech = RecordSpeech()
    anyio.run(process_turn, db, ServiceBundle(AnswerModel(answer), speech), session, turn)
    saved = db.get_turn(str(session["id"]), "id1")
    assert saved is not None
    return str(saved["final_text"]), speech.final


def test_structured_answer_selects_display_and_speech_fields(tmp_path: Path) -> None:
    payload = json.dumps({"display_text": "화면에 표시할 답변", "tts_text": "음성으로 읽을 답변", "internal": "숨겨진 데이터"}, ensure_ascii=False)
    display, spoken = run_turn(tmp_path, payload)
    assert display == "화면에 표시할 답변"
    assert spoken == "음성으로 읽을 답변"


def test_malformed_structured_answer_never_leaks_raw_transport(tmp_path: Path) -> None:
    display, spoken = run_turn(tmp_path, '{"display_text": "불완전한 내용"')
    assert display == INSUFFICIENT_MESSAGE
    assert spoken == INSUFFICIENT_MESSAGE


def test_normal_plain_answer_is_preserved_as_text(tmp_path: Path) -> None:
    display, spoken = run_turn(tmp_path, "의료진에게 확인해 주세요. [clinic:sample-clinic-1]")
    assert display == "의료진에게 확인해 주세요."
    assert spoken == "의료진에게 확인해 주세요."


def test_structured_answer_hides_source_markers_from_display_and_speech(tmp_path: Path) -> None:
    payload = json.dumps({"display_text": "화면 답변 [clinic:source-1]", "tts_text": "음성 답변 [patient:source-2]"}, ensure_ascii=False)
    display, spoken = run_turn(tmp_path, payload)
    assert display == "화면 답변"
    assert spoken == "음성 답변"


def test_inline_source_marker_still_validates_grounding() -> None:
    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return "안내입니다. [clinic:1]"

    config = OpenAIConfig(base_url="https://example.com/v1", api_key="fake", reasoning_model="high", allow_medical_data=True)
    provider = Provider(config)
    assert anyio.run(provider.reasoning, "question", [{"id": "clinic:1", "text": "source"}]) != INSUFFICIENT_MESSAGE
    assert anyio.run(provider.reasoning, "question", [{"id": "clinic:other", "text": "source"}]) == INSUFFICIENT_MESSAGE


def test_json_with_metadata_citations_is_accepted_when_the_source_is_real() -> None:
    payload = json.dumps({"display_text": "화면 답변", "tts_text": "음성 답변", "citations": ["clinic:1"]}, ensure_ascii=False)

    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return payload

    config = OpenAIConfig(base_url="https://example.com/v1", api_key="fake", reasoning_model="high", allow_medical_data=True)
    answer = anyio.run(Provider(config).reasoning, "question", [{"id": "clinic:1", "text": "source"}])
    assert answer == payload


def test_json_with_forged_metadata_citation_is_rejected() -> None:
    payload = json.dumps({"display_text": "화면 답변", "tts_text": "음성 답변", "citations": ["clinic:999"]}, ensure_ascii=False)

    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return payload

    config = OpenAIConfig(base_url="https://example.com/v1", api_key="fake", reasoning_model="high", allow_medical_data=True)
    answer = anyio.run(Provider(config).reasoning, "question", [{"id": "clinic:1", "text": "source"}])
    assert answer == INSUFFICIENT_MESSAGE


def test_json_without_citations_is_rejected_even_when_search_finds_a_source() -> None:
    payload = json.dumps({"display_text": "화면 답변", "tts_text": "음성 답변", "citations": []}, ensure_ascii=False)

    class Provider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return payload

    config = OpenAIConfig(base_url="https://example.com/v1", api_key="fake", reasoning_model="high", allow_medical_data=True)
    answer = anyio.run(Provider(config).reasoning, "question", [{"id": "clinic:1", "text": "source"}])
    assert answer == INSUFFICIENT_MESSAGE


def test_korean_fields_in_fenced_json_are_rendered_without_metadata(tmp_path: Path) -> None:
    display, spoken = run_turn(tmp_path, '```json\n{"표시":"확인해 주세요", "TTS":"확인 바랍니다", "debug":"secret"}\n```')
    assert display == "확인해 주세요"
    assert spoken == "확인 바랍니다"


def test_model_prose_wrapping_json_does_not_leak_metadata(tmp_path: Path) -> None:
    display, spoken = run_turn(tmp_path, '다음은 응답입니다: {"display":"안내 문장", "tts":"음성 문장", "debug":"내부 메모"}')
    assert display == "안내 문장"
    assert spoken == "음성 문장"


def test_double_encoded_json_does_not_leak_transport_syntax(tmp_path: Path) -> None:
    payload = json.dumps(json.dumps({"display_text": "화면 문장", "tts_text": "음성 문장"}, ensure_ascii=False), ensure_ascii=False)
    display, spoken = run_turn(tmp_path, payload)
    assert display == "화면 문장"
    assert spoken == "음성 문장"


def test_array_in_a_text_field_is_not_read_to_a_caller(tmp_path: Path) -> None:
    display, spoken = run_turn(tmp_path, '{"display_text":"[{\\"internal\\":\\"data\\"}]", "tts_text":"안내"}')
    assert display == INSUFFICIENT_MESSAGE
    assert spoken == INSUFFICIENT_MESSAGE


def test_call_api_and_tts_receive_only_their_designated_text(tmp_path: Path) -> None:
    payload = json.dumps({"display_text": "화면용 가상 응답", "tts_text": "음성용 가상 응답", "internal": "internal-secret"}, ensure_ascii=False)
    speech = RecordSpeech()
    db = Database(tmp_path / "call.sqlite")
    settings = Settings(environment="development", service_token="local-test", database_path=str(db.path))
    app = create_app(settings, db, ServiceBundle(AnswerModel(payload), speech))
    with TestClient(app, headers={"Authorization": "Bearer local-test"}) as client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의"}).raise_for_status()
        client.post(f"/calls/{call_id}/messages", json={"request_id": "r1", "text": "기침이 있어요"}).raise_for_status()
        response = client.get(f"/calls/{call_id}/messages/r1")
    assert response.json()["final_text"] == "화면용 가상 응답"
    assert speech.final == "음성용 가상 응답"
    assert "internal-secret" not in response.text
