import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app.call_interpret import classify_intent
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS, SplitLLM


class FakeLow:
    def __init__(self, response):
        self.response = response
        self.calls = []
    async def _complete(self, model, system, user):
        self.calls.append(user)
        return self.response
    class config:
        low_model = 'fake'
        allow_medical_data = True


@pytest.mark.anyio
async def test_decision_returns_structured_voice_controls_and_recommendation_mode():
    from app.call_interpret import classify_decision

    provider = FakeLow(json.dumps({'intent': 'recommend', 'recommendation_mode': 'next',
                                   'voice_controls': {'speed': 'slow', 'control_only': False}}))
    decision = await classify_decision(provider, 'caller', '다른 병원을 천천히 알려줘요', [])
    assert decision.intent == 'recommend'
    assert decision.recommendation_mode == 'next'
    assert decision.voice_controls is not None
    assert decision.voice_controls.speed == 'slow'
    assert decision.voice_controls.control_only is False


@pytest.mark.anyio
async def test_intent_accepts_single_fenced_json_but_not_surrounding_prose():
    provider = FakeLow('```json\n{"intent":"consultation","voice_controls":{"speed":"slow","control_only":true}}\n```')
    from app.call_interpret import classify_decision
    decision = await classify_decision(provider, 'caller', '천천히 말씀해 주세요', [])
    assert decision.intent == 'consultation'
    assert decision.voice_controls is not None and decision.voice_controls.control_only
    invalid = FakeLow('안내: ```json\n{"intent":"consultation"}\n```')
    assert await classify_intent(invalid, 'caller', '천천히 말씀해 주세요', []) == 'uncertain'


def test_interpret_exposes_only_region_quoted_by_caller(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'region.sqlite'))
    provider = FakeLow(json.dumps({'intent': 'consultation', 'region_text': '화순읍', 'region_only': True,
                                   'location_subject': 'other'}))
    app = create_app(settings, Database(settings.database_path), ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        result = client.post(f'/calls/{call_id}/interpret', json={'text': '어머니는 화순읍에 있어요'})
        assert result.json()['region_text'] == '화순읍'
        assert result.json()['location_subject'] == 'other'
        unquoted = client.post(f'/calls/{call_id}/interpret', json={'text': '어머니는 거기 있어요'})
        assert 'region_text' not in unquoted.json()


def test_same_place_requires_explicit_current_caller_statement(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'same-place.sqlite'))
    provider = FakeLow(json.dumps({'intent': 'consultation', 'location_subject': 'same_place'}))
    app = create_app(settings, Database(settings.database_path), ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        unsafe = client.post(f'/calls/{call_id}/interpret', json={'text': '어머니가 무릎이 아프세요'})
        assert 'location_subject' not in unsafe.json()
        yes = client.post(f'/calls/{call_id}/interpret', json={'text': '어머니가 제 옆에 계세요'})
        assert yes.json()['location_subject'] == 'same_place'
        no = client.post(f'/calls/{call_id}/interpret', json={'text': '어머니가 제 옆에 안 계세요'})
        assert 'location_subject' not in no.json()


def test_late_utterance_from_old_location_generation_is_not_interpreted(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'old-gen.sqlite'))
    provider = FakeLow('{"intent":"consultation"}')
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        early = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'legacy-early', 'text': '안녕하세요'})
        assert early.status_code == 200
        prior_calls = len(provider.calls)
        withdrawn = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        client.post(f'/calls/{call_id}/location-consent', json={'accepted': True, 'source_location_generation': 0,
                                                                'source_location_revision': withdrawn['location_revision']})
        response = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'late', 'text': '화순 동면에서 아파요', 'source_location_generation': 1})
        assistant = client.post(f'/calls/{call_id}/interpret', json={'subject': 'assistant', 'text': '화순 병원입니다', 'source_location_generation': 1})
        message = client.post(f'/calls/{call_id}/messages', json={'request_id': 'late', 'text': '화순 동면에서 아파요', 'source_location_generation': 1})
        unknown = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'legacy-late', 'text': '화순 동면에서 아파요'})
        assert assistant.status_code == message.status_code == unknown.status_code == 409
        assert client.post(f'/calls/{call_id}/interpret', json={'request_id': 'new', 'text': '무릎이 아파요', 'source_location_generation': 2}).status_code == 200
    assert response.status_code == 409
    assert len(provider.calls) == prior_calls + 1
    from contextlib import closing
    with closing(database.connect()) as con:
        assert con.execute('SELECT 1 FROM pending_utterances WHERE call_id=? AND request_id=?', (call_id, 'late')).fetchone() is None


def test_location_reaccept_is_local_and_requires_explicit_current_voice_consent(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'reaccept.sqlite'))
    provider = FakeLow('{"intent":"consultation"}')
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        withdrawn = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        question = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'q', 'text': '위치 다시 동의해도 돼요?', 'source_location_generation': 0,
                                                                    'source_location_revision': withdrawn['location_revision']})
        assert question.json()['intent'] != 'accept_location'
        accepted = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'yes', 'text': '위치 사용 다시 동의할게요', 'source_location_generation': 0,
                                                                    'source_location_revision': withdrawn['location_revision']})
        assert accepted.json()['intent'] == 'accept_location'
        assert accepted.json()['applied'] is True and accepted.json()['location_generation'] == 2
        assert accepted.json()['location_revision'] > withdrawn['location_revision']
    assert not provider.calls


def test_delayed_utterance_from_previous_revoked_epoch_is_rejected(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'old-epoch.sqlite'))
    provider = FakeLow('{"intent":"consultation"}')
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        consent = client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).json()
        first = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        again = client.post(f'/calls/{call_id}/location-consent', json={
            'accepted': True, 'source_location_generation': 0, 'source_location_revision': first['location_revision']}).json()
        last = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        assert consent['location_revision'] < first['location_revision'] < again['location_revision'] < last['location_revision']
        stale = client.post(f'/calls/{call_id}/interpret', json={
            'request_id': 'late-zero', 'text': '화순에서 아파요', 'source_location_generation': 0,
            'source_location_revision': first['location_revision']})
        current = client.post(f'/calls/{call_id}/interpret', json={
            'request_id': 'current-zero', 'text': '상담을 이어갈게요', 'source_location_generation': 0,
            'source_location_revision': last['location_revision']})
        assert stale.status_code == 409 and current.status_code == 200
    assert not provider.calls


def test_model_result_is_discarded_when_location_changes_during_call(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'model-race.sqlite'))
    database = Database(settings.database_path)

    class RevokingLow(FakeLow):
        call_id: str = ''

        async def _complete(self, model, system, user):
            from app.recommend_dashboard import RecommendationOutbox
            self.calls.append(user)
            RecommendationOutbox(database).set_location_consent(self.call_id, False)
            return '{"intent":"consultation","region_text":"화순","region_only":true}'

    provider = RevokingLow('')
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        provider.call_id = call_id
        consent = client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).json()
        response = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'changing', 'text': '화순',
            'source_location_generation': consent['location_generation'], 'source_location_revision': consent['location_revision']})
    assert response.status_code == 409
    assert len(provider.calls) == 1


def test_explicit_location_withdrawal_is_honored_even_with_stale_epoch(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'stale-withdraw.sqlite'))
    provider = FakeLow('{"intent":"consultation"}')
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        consent = client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).json()
        withdrawn = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        client.post(f'/calls/{call_id}/location-consent', json={'accepted': True, 'source_location_generation': 0,
            'source_location_revision': withdrawn['location_revision']})
        response = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'late-withdraw',
            'text': '위치 사용 취소할게요', 'source_location_generation': consent['location_generation'],
            'source_location_revision': consent['location_revision']})
    assert response.status_code == 200 and response.json()['intent'] == 'revoke_location'
    assert response.json()['location_generation'] == 0 and not provider.calls


@pytest.mark.anyio
async def test_provider_without_data_transfer_permission_receives_nothing():
    class Denied(FakeLow):
        class config:
            low_model = 'fake'
            allow_medical_data = False
    provider = Denied('{"intent":"consultation"}')
    assert await classify_intent(provider, 'caller', '기침이 납니다', []) == 'uncertain'
    assert provider.calls == []


@pytest.mark.anyio
async def test_contextual_recommendation_intent():
    provider = FakeLow('{"intent":"recommend"}')
    result = await classify_intent(provider, 'caller', '어디로 가면 좋을까요?', [])
    assert result == 'recommend'


@pytest.mark.anyio
async def test_degraded_visit_offer_remains_in_same_call_context():
    provider = FakeLow('{"intent":"recommend"}')
    history = [{'status': 'degraded', 'input_text': '무릎이 불편해요',
                'final_text': '가까운 병원을 찾아드릴까요?'}]
    assert await classify_intent(provider, 'caller', '네', history) == 'recommend'
    assert json.loads(provider.calls[0])['previous_turns'] == [
        {'caller': '무릎이 불편해요', 'assistant': '가까운 병원을 찾아드릴까요?'}
    ]


def test_explicit_department_selection_is_conservative():
    from app.call_interpret import explicit_specialty
    assert explicit_specialty('정형외과로 가고 싶어요') == '정형외과'
    assert explicit_specialty('내과는 싫어요') is None
    assert explicit_specialty('내과가 맞나요?') is None
    assert explicit_specialty('내과로 가도 될까요') is None
    assert explicit_specialty('예전에 정형외과 치료받았는데 가까운 병원 알려줘요') is None
    assert explicit_specialty('어머니가 정형외과에 다녔어요') is None
    assert explicit_specialty('내과요') == '내과'
    assert explicit_specialty('내과 말고 정형외과요') is None
    assert explicit_specialty('잘 모르겠어요') is None


def test_recommendation_intent_reports_only_explicit_department(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'calls.sqlite'))
    provider = FakeLow('{"intent":"recommend"}')
    app = create_app(settings, Database(settings.database_path), ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        selected = client.post(f'/calls/{call_id}/interpret', json={'text': '정형외과요', 'subject': 'caller'})
        unknown = client.post(f'/calls/{call_id}/interpret', json={'text': '잘 모르겠어요', 'subject': 'caller'})
    assert selected.json()['specialty'] == '정형외과'
    assert 'specialty' not in unknown.json()


@pytest.mark.anyio
async def test_bad_model_output_is_uncertain():
    provider = FakeLow('{"intent":"recommend", "extra": true}')
    assert await classify_intent(provider, 'caller', '잘 모르겠어요', []) == 'uncertain'


@pytest.mark.anyio
async def test_urgent_takes_priority_over_model():
    provider = FakeLow('{"intent":"end"}')
    assert await classify_intent(provider, 'caller', '숨을 못 쉬겠어요', []) == 'urgent'
    assert not provider.calls


@pytest.mark.anyio
async def test_split_pesticide_emergency_bypasses_model():
    provider = FakeLow('{"intent":"consultation"}')
    prior = [{'status': 'completed', 'input_text': '어제 농약을 뿌렸어요', 'final_text': '네'}]
    assert await classify_intent(provider, 'caller', '오늘 어지러워요', prior) == 'urgent'
    assert not provider.calls


@pytest.mark.anyio
async def test_assistant_classification_rejects_caller_only_intent():
    provider = FakeLow('{"intent":"recommend"}')
    assert await classify_intent(provider, 'assistant', '어디가 불편하신가요?', []) == 'uncertain'


@pytest.mark.anyio
async def test_only_recent_completed_conversation_is_included():
    provider = FakeLow('{"intent":"consultation"}')
    history = [{'status': 'completed', 'input_text': f'질문 {i}', 'final_text': '답변'} for i in range(5)]
    await classify_intent(provider, 'caller', '네', history)
    assert [turn['caller'] for turn in json.loads(provider.calls[0])['previous_turns']] == ['질문 2', '질문 3', '질문 4']


def test_interpretation_requires_prior_consent(tmp_path: Path):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'calls.sqlite'))
    provider = FakeLow('{"intent":"consultation"}')
    app = create_app(settings, Database(settings.database_path), ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        denied = client.post(f'/calls/{call_id}/interpret', json={'text': '불편해요', 'subject': 'caller'})
        assert denied.status_code == 403
        assert provider.calls == []
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
        result = client.post(f'/calls/{call_id}/interpret', json={'text': '불편해요', 'subject': 'caller'})
        assert result.json() == {'intent': 'consultation'}


@pytest.mark.parametrize('intent', ['revoke_location', 'revoke_consent'])
def test_interpreted_withdrawal_is_applied_before_response(tmp_path: Path, intent: str):
    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'calls.sqlite'))
    provider = FakeLow(json.dumps({'intent': intent}))
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        response = client.post(f'/calls/{call_id}/interpret', json={'text': '더는 이용하지 마세요', 'subject': 'caller'})
        assert response.json()['applied'] is True
        from app.recommend_dashboard import RecommendationOutbox
        assert RecommendationOutbox(database).location_generation(call_id) is None
        if intent == 'revoke_consent':
            assert client.post(f'/calls/{call_id}/messages', json={'request_id': 'x', 'text': '기침'}).status_code == 403
