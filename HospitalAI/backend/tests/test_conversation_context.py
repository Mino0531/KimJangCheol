import json
from pathlib import Path

from app.conversation_context import build_conversation_context, redact_superseded
from app.call_facts import FactEvidence
from app.db import Database


def test_correction_removes_superseded_source_from_context():
    old = {'id': 'old', 'session_id': 'call', 'request_id': 'old-request', 'input_text': '어제 시작했어요',
           'status': 'completed', 'final_text': '어제부터군요.'}
    correction = {'id': 'correction', 'session_id': 'call', 'request_id': 'new-request',
                  'input_text': '아니요 오늘 시작했어요', 'status': 'completed', 'final_text': '어제부터군요.'}
    current = {'id': 'current', 'session_id': 'call', 'request_id': 'current',
               'input_text': '정리해 주세요', 'status': 'pending'}
    superseded = (FactEvidence(key='onset', quote='어제 시작했어요', utterance_id='old-request'),)
    facts = (FactEvidence(key='onset', quote='오늘 시작했어요', utterance_id='new-request'),)

    history = redact_superseded([old, correction, current], superseded)
    context = build_conversation_context(history, current, facts=facts)

    assert '어제 시작했어요' not in context.prompt_text
    assert '어제 시작했어요' not in context.search_query
    assert '어제부터군요' not in context.prompt_text
    assert '오늘 시작했어요' in context.prompt_text


def test_long_call_retains_caller_correction_in_context(tmp_path: Path) -> None:
    db = Database(tmp_path / "long-call.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    for index, utterance in enumerate(["어제 시작했어요", "아니요 오늘 시작했어요"] + ["네 알겠어요"] * 18):
        turn, _ = db.create_turn(str(session["id"]), f"old-{index}", utterance)
        db.update_turn(str(turn["id"]), {"status": "completed"})
    current, _ = db.create_turn(str(session["id"]), "now", "지금까지 말씀 정리해 주세요")

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "아니요 오늘 시작했어요" in context.prompt_text


def test_very_old_uncorrected_first_turn_is_not_privileged(tmp_path: Path) -> None:
    db = Database(tmp_path / "very-long-call.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    for index, utterance in enumerate(["어제 시작했어요", "아니요 오늘 시작했어요"] + ["네 알겠어요"] * 40):
        turn, _ = db.create_turn(str(session["id"]), f"old-{index}", utterance)
        db.update_turn(str(turn["id"]), {"status": "completed"})
    current, _ = db.create_turn(str(session["id"]), "now", "지금까지 말씀 정리해 주세요")

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "어제 시작했어요" not in context.prompt_text
    assert "어제 시작했어요" not in context.search_query


def test_followup_uses_prior_symptom_and_question_from_same_session(tmp_path: Path) -> None:
    db = Database(tmp_path / "context.sqlite3")
    db.initialize(seed=True)
    alice = db.create_session("+821000000001", "sample-patient-1")
    bob = db.create_session("+821000000002", "sample-patient-2")
    previous, _ = db.create_turn(str(alice["id"]), "first", "기침이 나요")
    db.update_turn(str(previous["id"]), {"status": "completed", "final_text": "언제부터 기침이 나셨나요?"})
    other, _ = db.create_turn(str(bob["id"]), "other", "남의 개인 기록")
    db.update_turn(str(other["id"]), {"status": "completed", "final_text": "남의 답변"})
    current, _ = db.create_turn(str(alice["id"]), "second", "어제부터요")

    context = build_conversation_context(db.history(str(alice["id"])) + db.history(str(bob["id"])), current)

    assert "기침이 나요" in context.search_query
    assert "어제부터요" in context.search_query
    assert "언제부터 기침이 나셨나요?" in context.prompt_text
    assert "남의 개인 기록" not in context.search_query + context.prompt_text
    assert "남의 답변" not in context.prompt_text
    assert context.prompt_text.count("어제부터요") == 1


def test_history_includes_only_completed_turns_before_current(tmp_path: Path) -> None:
    db = Database(tmp_path / "turns.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    pending, _ = db.create_turn(str(session["id"]), "pending", "처리 전 문의")
    current, _ = db.create_turn(str(session["id"]), "current", "지금 문의")
    future, _ = db.create_turn(str(session["id"]), "future", "미래의 문의")
    db.update_turn(str(future["id"]), {"status": "completed", "final_text": "미래의 답변"})

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "처리 전 문의" not in context.prompt_text
    assert "미래의 문의" not in context.prompt_text
    assert "지금 문의" in context.prompt_text
    assert str(pending["id"]) not in context.prompt_text


def test_json_history_does_not_expose_hidden_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "json.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    old, _ = db.create_turn(str(session["id"]), "old", "기침")
    answer = json.dumps({"display_text": "언제부터인가요?", "internal": "비공개정보"}, ensure_ascii=False)
    db.update_turn(str(old["id"]), {"status": "completed", "final_text": answer})
    current, _ = db.create_turn(str(session["id"]), "new", "어제부터요")

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "언제부터인가요?" in context.prompt_text
    assert "비공개정보" not in context.prompt_text


def test_absent_current_turn_does_not_import_any_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "absent.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    old, _ = db.create_turn(str(session["id"]), "old", "기침")
    db.update_turn(str(old["id"]), {"status": "completed", "final_text": "언제부터인가요?"})
    current = {"id": "missing", "session_id": session["id"], "input_text": "새 문의"}

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "기침" not in context.search_query + context.prompt_text
    assert "새 문의" in context.prompt_text


def test_long_history_keeps_first_symptom_and_recent_turns_with_bounded_text(tmp_path: Path) -> None:
    db = Database(tmp_path / "bounded.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    for index in range(6):
        turn, _ = db.create_turn(str(session["id"]), f"old-{index}", f"symptom-{index} " + "x" * 5000)
        db.update_turn(str(turn["id"]), {"status": "completed", "final_text": "y" * 5000})
    current, _ = db.create_turn(str(session["id"]), "current", "follow-up")

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "symptom-0" in context.search_query
    assert "symptom-4" in context.search_query
    assert "symptom-5" in context.search_query
    assert "symptom-1" not in context.search_query
    assert len(context.prompt_text) < 6000


def test_malformed_previous_json_is_not_exposed_as_assistant_text(tmp_path: Path) -> None:
    db = Database(tmp_path / "invalid.sqlite3")
    db.initialize()
    session = db.create_session("+821000000003")
    old, _ = db.create_turn(str(session["id"]), "old", "기침")
    db.update_turn(str(old["id"]), {"status": "completed", "final_text": '{"internal":"secret"'})
    current, _ = db.create_turn(str(session["id"]), "new", "어제부터요")

    context = build_conversation_context(db.history(str(session["id"])), current)

    assert "기침" in context.search_query
    assert "secret" not in context.prompt_text
