"""Call navigation keeps a reported fall separate from a hospital-search request."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.call_knowledge import KnowledgeBase
from app.call_specialty import select_call_specialty
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommendation import HospitalCandidate, recommend
from app.services import TTSResult
from app.fall_guidance import has_fall, has_leg_fall_pain, person_label


class UnavailableModel:
    class config:
        low_model = 'test'
        allow_medical_data = True

    async def low(self, text: str) -> str:
        return '잠시만요.'

    async def _complete(self, model: str, system: str, user: str) -> str:
        return '{"intent":"recommend"}'

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        raise OSError('reasoning unavailable')


class AudioTTS:
    async def deliver(self, text: str, kind: str) -> TTSResult:
        return TTSResult(status='synthesized', kind=kind, text=text,
                         interruptible=False, audio=b'audio', mime_type='audio/mpeg')


@pytest.fixture
def call(tmp_path: Path):
    database = Database(tmp_path / 'guidance.sqlite')
    settings = Settings(environment='development', service_token='test', database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(UnavailableModel(), AudioTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer test'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
        yield client, call_id, database


def send(client: TestClient, call_id: str, request_id: str, text: str) -> dict:
    response = client.post(f'/calls/{call_id}/messages', json={'request_id': request_id, 'text': text})
    assert response.status_code == 202
    return client.get(f'/calls/{call_id}/messages/{request_id}').json()


def delivered(client: TestClient, call_id: str, request_id: str) -> None:
    response = client.post(f'/calls/{call_id}/messages/{request_id}/playback-complete')
    assert response.status_code == 200


def test_fall_gets_one_walking_question_even_if_model_is_unavailable(call) -> None:
    client, call_id, _ = call
    turn = send(client, call_id, 'fall', '어제 밭일 하다가 넘어졌는데 다리가 시큰시큰이야.')
    assert turn['guidance']['action'] == 'follow_up'
    assert turn['guidance']['question_id'] == 'fall_walking'
    assert turn['guidance']['specialty'] == '정형외과'
    assert turn['speech_text'] == turn['final_text']


def test_yes_answers_walking_before_a_search_is_offered(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어제 넘어져서 다리가 시큰거려요')
    delivered(client, call_id, 'fall')
    interpreted = client.post(f'/calls/{call_id}/interpret',
                              json={'request_id': 'walking', 'text': '네'}).json()
    assert interpreted['intent'] == 'consultation'
    result = send(client, call_id, 'walking', '네')
    assert result['guidance']['action'] == 'hospital_search_offer'
    assert result['guidance']['specialty'] == '정형외과'
    delivered(client, call_id, 'walking')
    consent = client.post(f'/calls/{call_id}/interpret',
                          json={'request_id': 'search', 'text': '네'}).json()
    assert consent['intent'] == 'recommend'


def test_unable_to_walk_answers_the_delivered_question_as_urgent(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어제 넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'unable', 'text': '아니요'}).json()
    assert result['intent'] == 'urgent'


def test_unknown_walking_answer_does_not_guess_a_department(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어제 넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = send(client, call_id, 'unknown', '잘 모르겠어요')
    assert result['guidance']['action'] == 'hospital_search_offer'
    assert result['guidance']['specialty'] is None


def test_interrupted_question_cannot_give_meaning_to_short_answer(call) -> None:
    client, call_id, database = call
    send(client, call_id, 'fall', '어제 넘어져 다리가 아파요')
    assert client.post(f'/calls/{call_id}/messages/fall/interrupt', json={}).status_code == 200
    result = send(client, call_id, 'unknown', '네')
    assert result.get('guidance', {}).get('question_id') != 'fall_walking_answered'
    assert database.get_turn(call_id, 'fall')['guidance_json'] is None


def test_retrieval_normalizes_fall_inflections_without_mutating_input() -> None:
    original = '어제 밭일 하다가 넘어졌는데 다리가 시큰시큰이야.'
    path = Path(__file__).resolve().parents[1] / 'data' / 'call_knowledge.json'
    results = KnowledgeBase(path).search(original)
    assert any('낙상' in row['text'] for row in results)
    assert original == '어제 밭일 하다가 넘어졌는데 다리가 시큰시큰이야.'


@pytest.mark.parametrize('text', [
    '어제 자빠져서 다리가 쑤셔요',
    '방금 미끄러져서 무릎이 욱신거려요',
    '넘어졌는데 발목이 시큰해요',
])
def test_fall_variants_receive_the_same_followup(call, text: str) -> None:
    client, call_id, _ = call
    assert send(client, call_id, 'fall', text)['guidance']['question_id'] == 'fall_walking'


@pytest.mark.parametrize('text', [
    '넘어지지 않았는데 다리가 아파요',
    '작년에 넘어졌는데 지금 다리가 아파요',
    '작년에 밭일하다가 넘어졌는데 지금 다리가 아파요',
    '어머니가 넘어지셨고 저는 다리가 아파요',
    '할머니가 넘어졌고 할아버지 다리가 아파요',
    '넘어졌지만 다리가 아프지 않아요',
    '넘어졌지만 다리가 시큰하지 않아요',
    '넘어졌지만 무릎은 안 아프고 머리가 아파요',
])
def test_fall_pattern_does_not_combine_denied_past_or_other_patient_facts(text: str) -> None:
    assert not has_leg_fall_pain(text)


def test_denied_fall_is_not_added_to_search_query() -> None:
    assert not has_fall('넘어지지 않았어요')


def test_a_new_fall_is_not_discarded_due_to_an_unrelated_old_complaint() -> None:
    assert has_leg_fall_pain('예전에 어깨가 아팠는데 어제 넘어져서 다리가 아파요')


@pytest.mark.parametrize('text', [
    '넘어졌는데 머리는 안 아프고 다리가 아파요',
    '넘어졌는데 무릎은 안 아프고 발목이 시큰해요',
    '넘어져서 다리가 아프지만 머리는 안 아파요',
])
def test_denial_of_another_body_part_does_not_hide_leg_pain(text: str) -> None:
    assert has_leg_fall_pain(text)


def test_grandfather_is_one_patient_not_a_second_father() -> None:
    assert person_label('할아버지') == 'grandfather'
    assert has_leg_fall_pain('할아버지가 넘어져서 다리가 아파요')


@pytest.mark.parametrize('utterance', ['어깨가 시큰시큰해요', '어깨가 쑤셔요'])
def test_search_understands_colloquial_pain_variants(tmp_path: Path, utterance: str) -> None:
    import json

    path = tmp_path / 'knowledge.json'
    path.write_text(json.dumps([{'id': 'x', 'category': '어깨 통증',
                                 'content': '관련단어: 어깨 통증. 병원에서 확인.'}], ensure_ascii=False), encoding='utf-8')
    assert KnowledgeBase(path).search(utterance)


@pytest.mark.parametrize('text', [
    '넘어져서 다리를 못 걸어요',
    '넘어진 뒤 다리가 휘었어요',
    '넘어져서 피가 많이 나요',
    '넘어진 뒤 걷기 힘들어요',
    '다쳐서 넘어졌는데 다리에 발을 못 디뎌요',
])
def test_explicit_danger_latches_urgent_before_a_regular_question(call, text: str) -> None:
    client, call_id, _ = call
    turn = client.post(f'/calls/{call_id}/messages', json={'request_id': 'urgent', 'text': text}).json()
    assert turn['urgent'] is True
    assert client.get(f'/calls/{call_id}').json()['urgent'] is True


def test_old_walking_difficulty_is_not_attributed_to_a_new_fall(call) -> None:
    client, call_id, _ = call
    turn = send(client, call_id, 'fall', '작년에 못 걸었는데 오늘 넘어져서 다리가 아파요')
    assert turn['guidance']['question_id'] == 'fall_walking'


def test_declining_delivered_walking_question_is_urgent_even_without_intent_endpoint(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져서 다리가 아파요')
    delivered(client, call_id, 'fall')
    turn = client.post(f'/calls/{call_id}/messages', json={'request_id': 'unable', 'text': '아니요'}).json()
    assert turn['urgent'] is True


def test_unknown_answer_keeps_search_general_after_offer_is_heard(call) -> None:
    client, call_id, database = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    send(client, call_id, 'unknown', '잘 모르겠어요')
    delivered(client, call_id, 'unknown')
    assert select_call_specialty(None, database.history(call_id)).specialty is None
    assert select_call_specialty('내과', database.history(call_id)).specialty == '내과'


def test_location_withdrawal_discards_delivered_question(call) -> None:
    client, call_id, database = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    assert client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).status_code == 200
    saved = database.get_turn(call_id, 'fall')
    assert saved is not None and saved['guidance_json'] is None and saved['guidance_delivered'] == 0


def test_confirmed_candidate_reaches_real_hospital_search(call, monkeypatch) -> None:
    client, call_id, _ = call
    observed: list[str | None] = []

    async def lookup(query, service_key):
        observed.append(query.specialty)
        return recommend(query, [HospitalCandidate(id='one', name='가상 병원', latitude=35.001,
                                                   longitude=126, specialties=('정형외과',), source='fixture')])

    monkeypatch.setattr('app.call_api.public_recommendations', lookup)
    send(client, call_id, 'fall', '넘어져서 다리가 시큰거려요')
    delivered(client, call_id, 'fall')
    send(client, call_id, 'walking', '네')
    delivered(client, call_id, 'walking')
    intent = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'search', 'text': '네'}).json()
    assert intent['intent'] == 'recommend'
    response = client.post(f'/calls/{call_id}/recommendations',
                           json={'latitude': 35, 'longitude': 126, 'location_consent': True})
    assert response.status_code == 200
    assert observed == [None]
    assert response.json()['specialty_source'] == 'unknown'


def test_unplayed_question_does_not_turn_yes_into_a_search(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'unplayed', 'text': '네'}).json()
    assert result['intent'] != 'recommend'


def test_interrupted_question_does_not_turn_yes_into_a_search(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    client.post(f'/calls/{call_id}/messages/fall/interrupt', json={})
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'interrupted', 'text': '네'}).json()
    assert result['intent'] != 'recommend'


def test_explicit_inability_after_an_interrupted_question_still_escalates(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    client.post(f'/calls/{call_id}/messages/fall/interrupt', json={})
    turn = client.post(f'/calls/{call_id}/messages', json={'request_id': 'unable', 'text': '못 걸어요'}).json()
    assert turn['urgent'] is True


def test_generated_department_selection_question_is_replaced(tmp_path: Path, monkeypatch) -> None:
    class MisleadingModel(UnavailableModel):
        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            return '{"display_text":"어느 진료과를 원하세요?", "tts_text":"어느 진료과를 원하세요?", "citations":["clinic:1"]}'

    database = Database(tmp_path / 'misleading.sqlite')
    settings = Settings(environment='development', service_token='test', database_path=str(database.path))
    monkeypatch.setattr('app.call_search.CallDatabase.search', lambda *_args: [{'id': 'clinic:1', 'type': 'clinic', 'text': 'fixture'}])
    with TestClient(create_app(settings, database, ServiceBundle(MisleadingModel(), AudioTTS())),
                    headers={'Authorization': 'Bearer test'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        turn = send(client, call_id, 'question', '속이 불편해요')
    assert turn['error'] == 'unsafe_department_question'
    assert turn['guidance']['action'] == 'hospital_search_offer'


def test_question_interrupted_after_history_snapshot_cannot_produce_a_followup(call, monkeypatch) -> None:
    from contextlib import closing
    from app import orchestrator

    client, call_id, database = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    original = orchestrator.guided_response

    def interrupt_during_processing(text, history):
        response = original(text, history)
        if text == '네':
            with closing(database.connect()) as con, con:
                con.execute("UPDATE turns SET status='interrupted', final_text=NULL, guidance_json=NULL, guidance_delivered=0 WHERE session_id=? AND request_id='fall'", (call_id,))
        return response

    monkeypatch.setattr(orchestrator, 'guided_response', interrupt_during_processing)
    result = send(client, call_id, 'answer', '네')
    assert result['status'] == 'interrupted'
    assert result['final_text'] is None


def test_later_question_invalidation_hides_derived_answer_and_audio(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    assert send(client, call_id, 'answer', '네')['guidance']['specialty'] == '정형외과'
    assert client.post(f'/calls/{call_id}/messages/fall/interrupt', json={}).status_code == 200
    answer = client.get(f'/calls/{call_id}/messages/answer').json()
    assert answer['status'] == 'interrupted'
    assert answer['final_text'] is None
    assert client.get(f'/calls/{call_id}/messages/answer/audio/final').status_code == 409


def test_walking_answer_with_medication_question_is_not_silently_replaced(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = send(client, call_id, 'medication', '네, 걸을 수 있는데 약을 먹어도 되나요?')
    assert result['status'] == 'degraded'
    assert result['error'] == 'retrieval_or_reasoning_unavailable'
    assert result['guidance']['action'] == 'answer'
    assert result['speech_text'] == result['final_text']


def test_walking_reply_about_a_relative_is_not_applied_to_the_first_patient(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'relative', 'text': '네, 어머니는 걸을 수 있어요'}).json()
    assert result['intent'] == 'consultation'
    assert result['location_subject'] == 'other'


def test_explicitly_switching_relatives_creates_a_new_patient_boundary(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어머니가 넘어져서 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'father', 'text': '아버지는 걸을 수 있어요'}).json()
    assert result['intent'] == 'consultation'
    assert result['location_subject'] == 'other'
    assert result['patient_changed'] is True


def test_relative_inability_does_not_borrow_the_caller_fall(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'relative-unable', 'text': '어머니는 못 걸어요'}).json()
    assert result['intent'] == 'consultation'
    assert result['location_subject'] == 'other'


def test_direct_relative_inability_does_not_latch_original_patient_urgent(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    response = client.post(f'/calls/{call_id}/messages',
                           json={'request_id': 'relative-unable', 'text': '어머니는 못 걸어요'})
    assert not response.json().get('urgent', False)
    assert client.get(f'/calls/{call_id}').json()['urgent'] is False


def test_explicit_relative_fall_with_inability_is_still_urgent(call) -> None:
    client, call_id, _ = call
    response = client.post(f'/calls/{call_id}/messages',
                           json={'request_id': 'relative-urgent', 'text': '어머니가 넘어져서 못 걸어요'})
    assert response.json()['urgent'] is True


def test_same_relative_cannot_walk_after_her_reported_fall_is_urgent(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어머니가 넘어져서 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'unable', 'text': '어머니는 못 걸어요'}).json()
    assert result['intent'] == 'urgent'


def test_direct_same_relative_inability_after_fall_is_urgent(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '어머니가 넘어져서 다리가 아파요')
    delivered(client, call_id, 'fall')
    turn = client.post(f'/calls/{call_id}/messages',
                       json={'request_id': 'unable', 'text': '어머니는 못 걸어요'}).json()
    assert turn['urgent'] is True


def test_ambiguous_multi_person_walking_reply_is_not_assigned(call) -> None:
    client, call_id, _ = call
    send(client, call_id, 'fall', '넘어져 다리가 아파요')
    delivered(client, call_id, 'fall')
    result = client.post(f'/calls/{call_id}/interpret',
                         json={'request_id': 'mixed', 'text': '어머니는 괜찮고 저는 걸을 수 있어요'}).json()
    assert result['intent'] == 'uncertain'
