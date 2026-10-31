"""Short voice prompts for the call flow."""

import base64
from typing import Literal

import httpx2
from fastapi import HTTPException
from fastapi.responses import Response

from app.db import Row
from app.orchestrator import SpeechOutput
from app.services import EMERGENCY_MESSAGE, PESTICIDE_EMERGENCY_MESSAGE, ServiceError

PromptKind = Literal[
    "consent", "consent_retry", "intent_retry", "declined", "revoked", "ready", "question", "urgent", "pesticide_urgent",
    "location_retry", "location_declined", "location_unavailable", "hospital_unavailable", "speech_unavailable", "goodbye",
]
CONSENT_PROMPT = (
    "전화번호와 건강 상담 내용, 위치를 이용합니다. "
    "상담 내용은 지자체가 보관하고, 확인 뒤 병원에 전할 수 있습니다. "
    "음성과 상담 내용은 외부 음성 및 AI 서비스에서도 처리합니다. "
    "좌표는 저장하지 않지만 병원과 거리로 대략적인 위치를 알 수 있습니다. "
    "동의하시나요?"
)
READY_PROMPT = "네, 어디가 불편하신가요?"
QUESTION_PROMPT = "어디가 불편하신가요? 말씀해 주세요."
DECLINED_PROMPT = "동의하지 않으셔서 상담을 마칩니다."
REVOKED_PROMPT = "동의를 철회하고 상담을 마칠게요. 이미 전달된 내용은 자동으로 회수되지 않습니다."
CONSENT_RETRY_PROMPT = "위치 이용을 포함한 안내 내용 전체에 동의하시나요? 동의 또는 아니라고 말씀해 주세요."
INTENT_RETRY_PROMPT = "말씀을 잘 못 알아들었어요. 다시 말씀해 주세요."
SPEECH_UNAVAILABLE_PROMPT = "음성을 확인하지 못해 상담을 마칠게요. 다시 전화해 주세요."
LOCATION_RETRY_PROMPT = (
    "병원을 찾을 때 위치를 이용해도 될까요? 좌표는 저장하지 않지만, 추천 병원과 거리 기록으로 "
    "대략적인 위치를 알 수 있습니다. 네 또는 아니요로 말씀해 주세요."
)
LOCATION_DECLINED_PROMPT = "괜찮습니다. 위치 없이 상담을 이어갈게요."
LOCATION_UNAVAILABLE_PROMPT = "위치를 확인하지 못했어요. 상담은 계속할게요."
HOSPITAL_UNAVAILABLE_PROMPT = "가까운 병원을 확인하지 못했어요. 병원이나 보건소에 문의해 주세요."
GOODBYE_PROMPT = "네, 상담을 마칠게요. 감사합니다."
SERVICE_NUMBER = "070-1122-3344"


def call_payload(call: Row) -> dict[str, str | bool]:
    state = str(call["state"])
    prompt = CONSENT_PROMPT
    if state == "accepted":
        prompt = READY_PROMPT
    if state == "declined":
        prompt = DECLINED_PROMPT
    urgent = bool(call.get("urgent"))
    if urgent:
        prompt = EMERGENCY_MESSAGE
    result: dict[str, str | bool] = {"id": str(call["id"]), "service_number": SERVICE_NUMBER,
                                    "state": state, "prompt": prompt, "urgent": urgent}
    if state == "accepted" and not urgent:
        result["question_prompt"] = QUESTION_PROMPT
    return result


async def render_prompt(tts: SpeechOutput, kind: PromptKind) -> Response:
    text = {
        "consent": CONSENT_PROMPT, "consent_retry": CONSENT_RETRY_PROMPT, "intent_retry": INTENT_RETRY_PROMPT,
        "declined": DECLINED_PROMPT, "revoked": REVOKED_PROMPT, "ready": READY_PROMPT, "question": QUESTION_PROMPT,
        "urgent": EMERGENCY_MESSAGE, "pesticide_urgent": PESTICIDE_EMERGENCY_MESSAGE,
        "location_retry": LOCATION_RETRY_PROMPT,
        "location_declined": LOCATION_DECLINED_PROMPT,
        "location_unavailable": LOCATION_UNAVAILABLE_PROMPT,
        "hospital_unavailable": HOSPITAL_UNAVAILABLE_PROMPT, "goodbye": GOODBYE_PROMPT,
        "speech_unavailable": SPEECH_UNAVAILABLE_PROMPT,
    }[kind]
    try:
        result = await tts.deliver(text, "prompt")
    except (ServiceError, httpx2.HTTPError, TimeoutError, OSError) as exc:
        raise HTTPException(503, "Speech synthesis unavailable") from exc
    if result.status != "synthesized" or not result.audio or result.mime_type is None:
        raise HTTPException(503, "Speech synthesis unavailable")
    return Response(content=result.audio, media_type=result.mime_type,
                    headers={"Cache-Control": "no-store", "X-Spoken-Text-Base64": base64.b64encode(text.encode('utf-8')).decode('ascii')})
