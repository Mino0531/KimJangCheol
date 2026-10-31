"""Local controls after withdrawing location access."""

import pytest

from app.location_private_mode import classify_private_intent, private_fallback


@pytest.mark.parametrize('text', ['이제 끊을게요', '상담 그만할게요', '네. 전화를 끊겠습니다'])
def test_clear_end_intent_is_honoured(text: str) -> None:
    assert classify_private_intent(text) == 'end'


@pytest.mark.parametrize('text', ['끊어도 돼요?', '아직 끊지 말고 기다려요', '고마워요', '병원에 가볼게요'])
def test_ambiguous_end_keeps_call_open(text: str) -> None:
    assert classify_private_intent(text) == 'consultation'


@pytest.mark.parametrize('text', [
    '동의 철회', '개인정보 제공 동의 취소할게요', '개인정보 제공 철회',
    '개인정보 동의를 지금 철회해 주세요', '지금 개인정보 동의를 철회해 주세요',
    '개인정보 수집 동의는 바로 취소해주세요', '정보 제공을 이제 철회해 주세요',
])
def test_explicit_consent_withdrawal_is_honoured(text: str) -> None:
    assert classify_private_intent(text) == 'revoke_consent'


def test_current_emergency_is_not_suppressed() -> None:
    assert classify_private_intent('숨을 못 쉬겠어요') == 'urgent'


def test_explicit_withdrawal_is_not_blocked_by_prior_emergency_text() -> None:
    assert classify_private_intent('개인정보 제공 철회', ('숨을 못 쉬겠어요',)) == 'revoke_consent'


@pytest.mark.parametrize('text', [
    '동의 철회?', '개인정보 제공 철회?', '개인정보 동의를 지금 철회해 주세요?',
    '동의를 철회하지 말고 지금 계속해주세요',
    '개인정보 동의를 지금 철회해 주세요라고 말했어요',
    '개인정보 동의를 지금 철회하면 병원을 찾나요',
    '“개인정보 동의를 지금 철회해 주세요”',
])
def test_withdrawal_question_does_not_withdraw_all_consent(text: str) -> None:
    assert classify_private_intent(text) != 'revoke_consent'


@pytest.mark.parametrize('text', ['화순군 동면에서 무릎이 아파요', '어머니가 혼자 계세요'])
def test_arbitrary_speech_remains_local_and_continues(text: str) -> None:
    assert classify_private_intent(text) == 'consultation'
    assert text not in private_fallback()


def test_local_fallback_rotates_three_fixed_messages_without_caller_input() -> None:
    from app.location_private_mode import private_fallback_for_turn

    messages = [private_fallback_for_turn(index) for index in range(3)]
    assert messages[0] == private_fallback()
    assert len(set(messages)) == 3
    assert private_fallback_for_turn(3) == messages[0]
    assert private_fallback_for_turn(-1) == messages[0]


@pytest.mark.parametrize('text', [
    '위치 다시 동의할게요', '위치정보 사용에 다시 동의합니다',
    '내 위치 다시 사용해도 됩니다', '위치 다시 써도 좋아요',
])
def test_explicit_current_location_consent_is_accepted(text: str) -> None:
    assert classify_private_intent(text) == 'accept_location'


@pytest.mark.parametrize('text', [
    '위치 다시 동의해도 되나요?', '위치 다시 사용하면 되나요',
    '위치 동의하지 않아요', '위치 사용을 다시 철회할게요',
    '지난번에 위치 동의했어요', '위치 동의하면 병원 찾나요',
    '위치 다시 동의할게요?', '위치 사용하지 마세요',
])
def test_ambiguous_or_negative_location_consent_is_not_accepted(text: str) -> None:
    assert classify_private_intent(text) != 'accept_location'


@pytest.mark.parametrize('text', [
    '위치 동의 철회해요', '위치 사용 취소할게요',
    '제 위치정보 이용을 철회합니다', '네, 위치 사용동의 취소해주세요',
    '위치 이용은 이제 취소해 주세요', '지금 제 위치정보 사용을 철회할게요',
    '위치 동의만 바로 취소합니다',
])
def test_explicit_current_location_withdrawal_is_applied(text: str) -> None:
    assert classify_private_intent(text) == 'revoke_location'


@pytest.mark.parametrize('text', [
    '위치 동의 철회해도 되나요?', '위치 사용 취소할게요?',
    '위치 동의 철회하지 마세요', '전에 위치 동의 취소했어요',
    '위치 사용 취소할게요라고 말했어요', '위치 동의 철회해요 무릎이 아파요',
    '위치 동의 철회하면 병원 찾나요',
    '위치 이용은 이제 취소해 주세요?', '위치 이용은 이제 취소하지 마세요',
    '위치 이용은 이제 취소해 주세요 무릎이 아파요',
    '“위치 이용은 이제 취소해 주세요”',
])
def test_ambiguous_location_withdrawal_is_not_applied(text: str) -> None:
    assert classify_private_intent(text) != 'revoke_location'


def test_current_location_withdrawal_is_not_blocked_by_prior_emergency() -> None:
    assert classify_private_intent('위치 사용 취소할게요', ('숨을 못 쉬겠어요',)) == 'revoke_location'
