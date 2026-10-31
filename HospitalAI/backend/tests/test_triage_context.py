"""Emergency keyword routing must respect explicit local negation."""

import pytest

from app.services import emergency_detected, pesticide_dizziness
from app.specialty import suggest_specialty


@pytest.mark.parametrize("utterance", [
    "가슴이 아프지 않아요",
    "가슴이 아프진 않아요",
    "호흡곤란은 없어요. 기침만 나요",
    "농약 때문에 어지럽지는 않아요",
    "농약 때문에 어지럽진 않아요",
    "피를 조금 흘렸지만 지금은 멈췄어요",
    "피를 흘렸지만 멈췄어요",
    "아버지가 작년에 쓰러졌어요",
    "작년에 쓰러졌고 지금 기침만 나요",
    "어제 농약 뿌렸지만 안 어지러워요",
    "아버지가 쓰러졌다는 말은 아닙니다",
    "농약을 마시지 않았어요",
    "농약을 먹은 적 없어요",
    "작년에 농약을 마셨고 지금은 괜찮아요",
    "농약을 뿌렸지만 토하고 있지는 않아요",
    "농약은 입에 안 들어갔어요",
    "농약이 입에 들어간 적은 없습니다",
    "농약을 마셨는지 몰라요",
    "농약을 흡입하지 않았어요",
])
def test_explicitly_negated_or_stopped_symptoms_are_not_emergency(utterance: str) -> None:
    assert emergency_detected(utterance) is False
    assert suggest_specialty(utterance).status != "urgent"


@pytest.mark.parametrize("utterance", [
    "가슴은 아프지 않지만 숨을 못 쉬어요",
    "어머니가 지금 숨을 못 쉬어요",
    "가슴이 아프고 식은땀이 나요",
    "농약 뿌린 뒤 어지러워요",
    "피가 멈추지 않아요",
    "아버지가 작년에 쓰러졌고 지금 또 쓰러졌어요",
    "작년에 어머니가 쓰러졌지만 지금 숨을 못 쉬어요",
    "작년에 무릎 수술했는데 가슴이 아파요",
    "몇 년 전에 기침했는데 숨을 못 쉬어요",
    "숨 못 쉬어요",
    "가슴 아파요",
    "피가 많이 나요",
    "피가 안 멈춰요",
    "어머니가 쓰러져 있어요",
    "숨을 쉴 수가 없어요",
    "숨이 안 쉬어져요",
    "가슴이 아프지 않은 건 아니에요",
    "안 어지러운 게 아니에요. 어제 농약 뿌렸어요",
    "안 어지러운데 숨 못 쉬어요",
    "이틀 동안 어지러워요. 어제 농약 뿌렸어요",
    "불안, 가슴이 아파요",
    "불안 가슴이 아파요",
    "농약을 마셨어요",
    "농약을 먹었어요",
    "농약을 흡입했어요",
    "농약 뿌리고 토하고 있어요",
    "농약이 입에 들어갔어요",
    "농약 뿌리고 구역질이 나요",
    "농약 뿌리고 몸이 떨리고 숨이 차요",
    "농약을 뿌렸는데 안 어지럽지 않아요",
    "농약을 마셨는지 몰라도 토하고 있어요",
    "작년에 쓰러졌는데 지금도 쓰러져 있어요",
])
def test_another_positive_or_current_emergency_is_not_suppressed(utterance: str) -> None:
    assert emergency_detected(utterance) is True


def test_negated_dizziness_does_not_trigger_pesticide_emergency() -> None:
    assert pesticide_dizziness("농약을 뿌렸지만 어지럽지는 않아요") is False


def test_denied_pesticide_exposure_does_not_trigger_pesticide_emergency() -> None:
    assert pesticide_dizziness("농약을 안 뿌렸는데 어지러워요") is False


def test_double_negative_does_not_suppress_possible_emergency() -> None:
    assert emergency_detected("호흡곤란이 없지는 않아요") is True
