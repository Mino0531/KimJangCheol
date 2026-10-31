"""Exact nonclinical utterances allowed without medical source citations."""

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.response_format import GeneratedAnswer

SERVICE_RESPONSES: Final = {
    "acknowledgement": "네, 더 궁금한 점이 있으면 말씀해 주세요.",
    "hold": "네, 천천히 말씀해 주세요. 듣고 있을게요.",
    "identity": "병원 방문 준비를 돕는 AI 안내 서비스예요. 궁금한 점을 말씀해 주세요.",
    "end_question": "통화를 마치시려면 끊겠다고 말씀해 주세요. 더 궁금한 점이 있으면 이어서 말씀해 주세요.",
}

SYMPTOM_OFFER: Final = '네, 말씀해 주셔서 고맙습니다. 가까운 병원을 찾아드릴까요?'
_SYMPTOM_WORD: Final = re.compile(
    r'아프|아파|아픔|통증|쑤시|쑤셔|저리|저려|시리|쓰리|쓰려|불편|'
    r'어지럽|어지러|어질|기침|가래|콧물|두통|설사|구토|토했|열이|열나|발열|'
    r'가렵|가려|붓|부었|메스껍|울렁|답답|저림|따끔|피곤|힘이없'
)
_NON_SYMPTOM_REQUEST: Final = re.compile(
    r'[?？]|약(?!간)|복용|먹|섭취|파스|붙여|처방|진단|원인|치료|병명|질환|중독|'
    r'수술|검사|응급|위험|119|낫|괜찮|안전|해열제|진통제|항생제|소염제|영양제|연고|주사|'
    r'어떻|어쩌|무엇|무슨|뭐|뭘|왜|이유|어디|얼마|몇\s*번|어떤|가능|되나|되나요|'
    r'될까|인가|났나요|했나요|있나요|없나요|까요|습니까|는지|해야|가야'
)
_SIMPLE_REMAINDER: Final = re.compile(
    r'(?:(?:저기|그러니까|그게|저는|네|예|어|음|아|저|요|어제부터|오늘부터|그제부터|'
    r'며칠전부터|몇일전부터|지금부터|아까부터|이틀전부터|사흘전부터)|[\s,.!。！])*'
)
_SIMPLE_CALLER_CHARACTERS: Final = re.compile(r'[가-힣ㄱ-ㅎㅏ-ㅣ0-9\s,.!。！~…-]+')


class _ReportedFact(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    key: Literal['symptom', 'onset', 'specialty', 'caller_role', 'preparation']
    quote: str = Field(min_length=1, max_length=250)
    status: Literal['reported']


class _EvidencePayload(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    current_caller: str = Field(min_length=1, max_length=10000)
    reported_facts: tuple[_ReportedFact, ...] = Field(max_length=20)


def _simple_reported_symptom(caller: str, facts: tuple[_ReportedFact, ...]) -> bool:
    """Fail closed unless the entire utterance is a simple reported symptom."""
    if (len(caller) > 200 or _SIMPLE_CALLER_CHARACTERS.fullmatch(caller) is None
            or _NON_SYMPTOM_REQUEST.search(caller)):
        return False
    excerpts: list[str] = []
    symptom_found = False
    for fact in facts:
        quote = fact.quote
        if not quote.strip() or quote not in caller:
            continue
        if fact.key == 'symptom' and _SYMPTOM_WORD.search(quote):
            symptom_found = True
            excerpts.append(quote)
        if fact.key == 'onset':
            excerpts.append(quote)
    if not symptom_found:
        return False
    remainder = caller
    for excerpt in sorted(excerpts, key=len, reverse=True):
        remainder = remainder.replace(excerpt, '', 1)
    return _SIMPLE_REMAINDER.fullmatch(remainder) is not None


def is_grounded_service_response(answer: GeneratedAnswer, question: str) -> bool:
    """Accept only exact service replies and caller-evidenced symptom offers."""
    if answer.display != answer.speech or answer.citations:
        return False
    if answer.display in SERVICE_RESPONSES.values():
        return True
    if answer.display != SYMPTOM_OFFER:
        return False
    _, separator, encoded = question.partition('\n')
    if not separator:
        return False
    try:
        payload = _EvidencePayload.model_validate_json(encoded)
    except ValidationError:
        return False
    return _simple_reported_symptom(payload.current_caller, payload.reported_facts)

SERVICE_RESPONSE_INSTRUCTION: Final = (
    "현재 발화가 새로운 의료·병원 사실을 묻지 않는 순수 서비스 대화라면, "
    "감사나 알겠다는 답에는 acknowledgement, 기다려 달라면 hold, AI인지 물으면 identity, "
    "통화를 끊어도 되느냐는 질문에는 end_question 문장을 사용한다. "
    "이 경우 display_text와 tts_text는 해당 문장을 원문 그대로 쓰고 citations는 빈 배열로 둔다. "
    "동시에 증상이나 병원·준비물 질문을 말하면 이 예외를 쓰지 말고 자료에 근거해 답한다. "
    "다른 문장을 추가하거나 의료 조언을 이 예외로 답하지 않는다. 허용 문장: "
    + " ".join(f"{key}={value}" for key, value in SERVICE_RESPONSES.items())
    + " 현재 발화의 reported_facts에 실제 인용된 symptom이 있고, 새 의학 질문이 없는 단순 증상 발화라면 "
      "근거 없는 증상 해석이나 의학 사실을 덧붙이지 말고 다음 문장만 사용한다. "
      f"display_text와 tts_text는 정확히 {SYMPTOM_OFFER} 이며 citations는 빈 배열이다. "
      "사용자가 증상과 함께 약물·진단·원인·응급 여부 등 구체적인 의학 질문을 했다면 이 문장을 사용하지 않는다. "
      "의학 질문에는 확인된 자료로 답하거나 자료가 부족하면 모른다고 밝힌다."
)
