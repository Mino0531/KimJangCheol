"""Bounded, non-diagnostic navigation for a reported fall with leg pain.

The walking question and urgent escalation are informed by:
https://www.nhs.uk/conditions/broken-leg/ and
https://www.nhs.uk/conditions/hip-fracture/
"""

import re
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db import Row
from app.emergency import _affirmed_occurrence, emergency_detected

_FALL: Final = ('넘어졌', '넘어져', '넘어진', '넘어지', '넘어짐', '자빠졌', '자빠져', '자빠진',
                '미끄러졌', '미끄러져', '미끄러진', '엉덩방아', '낙상')
_LEG: Final = ('다리', '무릎', '발목', '허벅지', '엉덩이', '고관절', '사타구니')
_BODY_PART: Final = re.compile(r'사타구니|고관절|허벅지|엉덩이|발목|손목|다리|무릎|머리|허리|어깨|가슴|치아|이빨|잇몸|얼굴|복부|배|팔|손|발|목|귀|눈')
_PAIN: Final = ('아파', '아프', '아픈', '통증', '시큰', '쑤셔', '쑤시', '욱신', '저려', '저리')
_NEGATED_PAIN: Final = re.compile(
    r'(?:안|못)(?:아프|아파|시큰|쑤시|쑤셔|욱신|저리|저려)|'
    r'(?:아프|아파|아픈|통증|시큰|쑤시|쑤셔|욱신|저리|저려)'
    r'(?:거리지|거리진|하지|하진|지|진|은|는)?(?:않|안|없|아니)'
)
_UNABLE: Final = ('못걸', '못걷', '걷지못', '걷기힘들', '걸을수없', '걸을수가없', '보행이안',
                  '딛을수없', '딛지못', '못디뎌', '못딛')
_DEFORMED: Final = ('다리가휘었', '다리휘었', '다리가꺾였', '다리꺾였', '다리가뒤틀렸', '뼈가튀어나', '뼈가보여')
_OLD_TIME: Final = re.compile(r'작년|재작년|지난해|몇년전|수년전|오래전|예전|과거')
_RECENT_TIME: Final = re.compile(r'지금|오늘|어제|그제|방금|아까|최근|이번주|지난주|며칠전|이틀전')
_OTHER_PERSON: Final = re.compile(r'할아버지|할머니|어머니|아버지|엄마|아빠|남편|아내|딸|아들|친구')
_PERSON_LABELS: Final = {'어머니': 'mother', '엄마': 'mother', '아버지': 'father', '아빠': 'father',
                         '할머니': 'grandmother', '할아버지': 'grandfather', '남편': 'husband',
                         '아내': 'wife', '딸': 'daughter', '아들': 'son', '친구': 'friend'}


class Guidance(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    action: Literal['follow_up', 'hospital_search_offer', 'urgent', 'answer']
    question_id: Literal['fall_walking'] | None = None
    specialty: str | None = Field(default=None, max_length=50)


@dataclass(frozen=True, slots=True)
class GuidedResponse:
    text: str
    guidance: Guidance


def _current_mention(compact: str, terms: tuple[str, ...], *, start: int = 0,
                     end: int | None = None, pain: bool = False) -> bool:
    for term in terms:
        for match in re.finditer(re.escape(term), compact):
            if match.start() < start or (end is not None and match.start() >= end):
                continue
            prefix = compact[max(0, match.start() - 60):match.start()]
            suffix = compact[match.end():match.end() + 20]
            old = list(_OLD_TIME.finditer(prefix))
            recent = list(_RECENT_TIME.finditer(prefix))
            if old and (not recent or old[-1].start() > recent[-1].start()):
                if not re.search(r'지금도|여전히|아직도', suffix):
                    continue
            fragment = prefix[-20:] + term + suffix
            if pain:
                near = prefix[-4:] + term + suffix
                target_start = len(prefix[-4:])
                target_end = target_start + len(term)
                if any(item.start() < target_end and item.end() > target_start
                       for item in _NEGATED_PAIN.finditer(near)):
                    continue
            if _affirmed_occurrence(fragment, term):
                return True
    return False


def has_fall(text: str) -> bool:
    compact = re.sub(r'\s+', '', text.lower())
    return _current_mention(compact, _FALL)


def has_leg_fall_pain(text: str) -> bool:
    compact = re.sub(r'\s+', '', text.lower())
    caller = re.search(r'저는|제가|내가|나는|제다리|내다리', compact) is not None
    relative_count = len(_person_labels(compact))
    if int(caller) + relative_count > 1 or not has_fall(text):
        return False
    parts = list(_BODY_PART.finditer(compact))
    for index, part in enumerate(parts):
        if part.group() not in _LEG:
            continue
        end = parts[index + 1].start() if index + 1 < len(parts) else len(compact)
        if _current_mention(compact, _PAIN, start=part.end(), end=end, pain=True):
            return True
    return False


def colloquial_pain(text: str) -> bool:
    compact = re.sub(r'\s+', '', text.lower())
    return _current_mention(compact, ('시큰', '쑤셔', '쑤시', '욱신'), pain=True)


def fall_urgent(text: str) -> bool:
    """Escalate only an affirmed, current inability to walk or major deformity."""
    compact = re.sub(r'\s+', '', text.lower())
    return _current_mention(compact, (*_UNABLE, *_DEFORMED))


def mentions_other_person(text: str) -> bool:
    return _OTHER_PERSON.search(text) is not None


def _person_labels(text: str) -> set[str]:
    return {_PERSON_LABELS[match.group()] for match in _OTHER_PERSON.finditer(text)}


def person_label(text: str) -> str | None:
    found = _person_labels(text)
    return next(iter(found)) if len(found) == 1 else None


def same_relative_with_fall(text: str, previous_text: str) -> bool:
    if re.search(r'저는|제가|내가|나는', text) or re.search(r'저는|제가|내가|나는', previous_text):
        return False
    person = person_label(text)
    return person is not None and person == person_label(previous_text) and has_fall(previous_text)


def walking_answer(text: str) -> Literal['yes', 'no', 'unknown', 'other']:
    compact = re.sub(r'[\s,.!?。！？]+', '', text.lower())
    if mentions_other_person(compact):
        return 'other'
    if any(term in compact for term in ('모르', '몰라', '안걸어봤', '안걸어보', '시도안', '해보지않')):
        return 'unknown'
    if fall_urgent(text) or compact in {'아니', '아니요', '아뇨', '아니에요', '못해요', '못해', '못걸어요'}:
        return 'no'
    if compact in {'네', '예', '응', '그래요', '맞아요', '맞습니다', '걸어요', '걸을수있어요'}:
        return 'yes'
    if re.search(r'(?:걸을수|걸을수가|걷는건|걷는것은)(?:는|은)?있', compact):
        return 'yes'
    return 'other'


def parse_guidance(row: Row) -> Guidance | None:
    raw = row.get('guidance_json')
    if not isinstance(raw, str):
        return None
    try:
        return Guidance.model_validate_json(raw)
    except ValidationError:
        return None


def visible_guidance(row: Row) -> Guidance | None:
    """Use only an uninterrupted question whose final audio completed playback."""
    if (row.get('status') not in ('completed', 'degraded') or row.get('guidance_delivered') != 1
            or not row.get('final_text')):
        return None
    return parse_guidance(row)


def last_guidance(history: list[Row]) -> Guidance | None:
    return visible_guidance(history[-1]) if history else None


def asks_patient_specialty(text: str) -> bool:
    """Reject generated questions that delegate department selection to the caller."""
    compact = re.sub(r'\s+', '', text)
    return bool(re.search(
        r'(?:원하|희망|가고싶|가실|찾으시).{0,15}진료과|'
        r'진료과.{0,20}(?:원하|희망|어디|선택|있으|있나요|있어|정하)|'
        r'(?:어느|어떤|무슨)(?:진료)?과(?:로|를|에)?(?:가|원|선택)', compact))


def medical_question(text: str) -> bool:
    """Recognize requests that need an answer rather than a fixed service offer."""
    compact = re.sub(r'\s+', '', text)
    topic = re.search(r'약|복용|처방|진통제|파스|연고|치료|수술|검사|진단|엑스레이|원인|골절', compact)
    question = re.search(r'[?？]|먹어|먹으|붙여|발라|될까|되나|괜찮|필요|해야|인가|어떻|무엇|뭐|왜', compact)
    return topic is not None and question is not None


def guided_response(text: str, history: list[Row]) -> GuidedResponse | None:
    """Return a fixed navigation response, never a diagnosis or treatment."""
    prior = last_guidance(history)
    if prior is not None and prior.action == 'follow_up' and prior.question_id == 'fall_walking':
        if medical_question(text):
            return None
        reply = walking_answer(text)
        if reply == 'yes' and not emergency_detected(text):
            return GuidedResponse('다친 다리는 의료기관에서 확인받으시는 게 좋겠어요. 가까운 종합병원을 찾아드릴까요?',
                                  Guidance(action='hospital_search_offer', specialty='정형외과'))
        if reply == 'unknown':
            return GuidedResponse('정확한 상태는 의료기관에서 확인받으시는 게 좋겠어요. 가까운 의료기관을 찾아드릴까요?',
                                  Guidance(action='hospital_search_offer'))
        return None
    if has_leg_fall_pain(text) and not fall_urgent(text) and not emergency_detected(text):
        return GuidedResponse('넘어진 뒤 다리가 아프시군요. 지금 걸을 수 있으신가요?',
                              Guidance(action='follow_up', question_id='fall_walking', specialty='정형외과'))
    return None
