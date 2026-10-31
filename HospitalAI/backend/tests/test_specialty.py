"""Conservative symptom-to-department suggestions for the call workflow."""

import pytest

from app.specialty import suggest_specialty


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("기침이 계속 나요", "내과"),
        ("열이 나고 기침이 나요", "내과"),
        ("무릎이 아파요", "정형외과"),
        ("허리가 쑤셔요", "정형외과"),
        ("이가 아파요", "치과"),
    ],
)
def test_clear_single_symptom_suggests_a_department(statement: str, expected: str) -> None:
    result = suggest_specialty(statement)
    assert result.specialty == expected
    assert result.status == "suggested"


@pytest.mark.parametrize(
    "statement",
    ["잘 모르겠어요", "기침은 없고 무릎이 아파요", "기침하고 무릎이 아파요", "기침이 아닌 것 같아요", "허리 검사", "농약 사용법을 알고 싶어요", "살충제랑 기침이 관계가 있나요"],
)
def test_unknown_negated_or_multi_department_input_is_not_guessed(statement: str) -> None:
    result = suggest_specialty(statement)
    assert result.specialty is None
    assert result.status == "unknown"


@pytest.mark.parametrize(
    "statement",
    ["숨을 못 쉬고 기침해요", "농약 뿌린 뒤 어지러워요", "가슴이 아프고 기침이 나요", "한쪽이 마비됐어요"],
)
def test_urgent_language_does_not_produce_a_routine_department(statement: str) -> None:
    result = suggest_specialty(statement)
    assert result.specialty is None
    assert result.status == "urgent"
