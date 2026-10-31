import pytest

from backend.clinical_digest import Digest, EvidenceError, caller_evidence, grounded_digest


def test_only_caller_words_are_used_for_ai_summary():
    record = {"events": [{"user_text": "무릎이 시큰해요", "assistant_text": "당뇨병입니다"},
                          {"user_text": "무릎이 시큰해요"}, {"user_text": ""}]}
    assert caller_evidence(record) == "무릎이 시큰해요"


def test_ai_symptom_requires_a_quote_in_the_callers_words():
    digest = Digest.model_validate({"symptoms": [{"body_part": "무릎", "description": "시큰거림", "quote": "무릎이 시큰해요"}]})
    assert grounded_digest(digest, "어제부터 무릎이 시큰해요").symptoms[0].body_part == "무릎"
    with pytest.raises(EvidenceError):
        grounded_digest(digest, "어깨가 아파요")


def test_unanswered_user_words_are_included_without_ai_answers():
    record = {"events": [], "unanswered_utterances": [{"user_text": "어제 넘어졌어요"}]}
    assert caller_evidence(record) == "어제 넘어졌어요"
