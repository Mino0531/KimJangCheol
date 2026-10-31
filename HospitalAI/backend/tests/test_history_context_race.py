"""Copied history cannot restore withdrawn hospital-derived assistant text."""

import json
from contextlib import closing
from pathlib import Path

import pytest

from app.orchestrator import ServiceBundle, process_turn
from app.db import Database, Row
from app.recommend_dashboard import RecommendationOutbox
from app.recommendation_context import load_recommendation_context
from app.recommendation_turns import bind_context_turn
from app import conversation_admission
from test_recommendation_context_race import RevocationServices, prepared
from test_visit_followup_context import setup_client, queue_hospital


class RecordingHistory(RevocationServices):
    """Record the model boundary and emit only synthetic source-grounded text."""

    def __init__(self, database: Database, call_id: str) -> None:
        super().__init__(database, call_id, "none", False)
        self.prompts: list[str] = []

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        self.prompts.append(text)
        answer = ("가상 병원 번호는 062-111-2222입니다." if "062-111-2222" in text
                  else "말씀하신 기침은 진료 때 알려 주세요.")
        return json.dumps({"display_text": answer, "tts_text": answer,
                           "citations": [sources[0]["id"]]}, ensure_ascii=False)


@pytest.mark.anyio
async def test_copied_history_is_refreshed_after_location_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, call_id = prepared(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO clinic_records(id,text,source) VALUES ('fixture','기침 진료 때 기록을 알려 주세요','가상 자료')")
    first, _ = database.create_turn(call_id, "first", "기침 때문에 그 병원 번호 다시 알려줘")
    services = RecordingHistory(database, call_id)
    session = database.get_session(call_id)
    assert session is not None
    await process_turn(database, ServiceBundle(services, services), session, first)
    assert "062-111-2222" in str(database.get_turn(call_id, "first")["final_text"])
    prior_prompt_count = len(services.prompts)
    second, _ = database.create_turn(call_id, "second", "그 번호 다시요")
    original_history = database.history
    withdrawn: list[bool] = []

    def stale_history(session_id: str) -> list[Row]:
        snapshot = original_history(session_id)
        RecommendationOutbox(database).set_location_consent(call_id, False)
        withdrawn.append(True)
        return snapshot

    monkeypatch.setattr(database, "history", stale_history)
    await process_turn(database, ServiceBundle(services, services), session, second)

    assert withdrawn
    assert len(services.prompts) == prior_prompt_count
    saved = database.get_turn(call_id, "second")
    assert saved is not None and saved["status"] == "interrupted"
    assert "062-111-2222" not in str(saved["final_text"])
    assert database.get_turn(call_id, "first")["status"] == "interrupted"


def test_intent_model_refreshes_copied_history_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, database, low, _ = setup_client(tmp_path)
    with client:
        call_id = client.post("/calls", json={"phone": "+821000000001"}).json()["id"]
        client.post(f"/calls/{call_id}/consent", json={"text": "동의합니다"})
        queue_hospital(database, call_id)
        turn, _ = database.create_turn(call_id, "prior", "기침 때문에 병원 번호요")
        recommendation = load_recommendation_context(database, call_id)
        assert recommendation is not None and bind_context_turn(database, str(turn["id"]), recommendation)
        database.update_turn(str(turn["id"]), {"status": "completed", "final_text": "가상 병원 번호는 062-000-0000입니다.",
            "sources_json": '[{"id":"clinic:recent-recommendation","type":"clinic"}]'})
        original_history = database.history
        withdrawn: list[bool] = []

        def stale_history(session_id: str) -> list[Row]:
            snapshot = original_history(session_id)
            RecommendationOutbox(database).set_location_consent(call_id, False)
            withdrawn.append(True)
            return snapshot

        monkeypatch.setattr(database, "history", stale_history)
        response = client.post(f"/calls/{call_id}/interpret", json={"text": "그 번호 다시요"})

    assert response.status_code == 409 and withdrawn
    assert not low.requests


def test_intent_rechecks_location_after_initial_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import call_api
    client, database, low, _ = setup_client(tmp_path)
    with client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        queue_hospital(database, call_id)
        original = call_api.admit_conversation

        def admission_then_withdraw(database, scope, history):
            admitted = original(database, scope, history)
            if scope.utterance_id is None:
                RecommendationOutbox(database).set_location_consent(call_id, False)
            return admitted

        monkeypatch.setattr(call_api, 'admit_conversation', admission_then_withdraw)
        response = client.post(f'/calls/{call_id}/interpret', json={'request_id': 'race', 'text': '그 병원 번호요',
                                                                   'source_location_generation': 1})

    assert response.status_code == 409
    assert not low.requests


@pytest.mark.anyio
async def test_history_dependency_cancels_followup_even_without_fresh_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, call_id = prepared(tmp_path)
    with closing(database.connect()) as con, con:
        con.execute("INSERT INTO clinic_records(id,text,source) VALUES ('fixture','기침 진료 때 알려 주세요','가상 자료')")
    first, _ = database.create_turn(call_id, "first", "기침 때문에 병원 번호요")
    session = database.get_session(call_id)
    assert session is not None
    services = RecordingHistory(database, call_id)
    await process_turn(database, ServiceBundle(services, services), session, first)
    second, _ = database.create_turn(call_id, "second", "그 번호 다시요")
    monkeypatch.setattr(conversation_admission, "load_recommendation_context", lambda *args, **kwargs: None)
    original_reasoning = services.reasoning

    async def reasoning_after_withdrawal(text: str, sources: list[dict[str, str]]) -> str:
        services.revoke()
        return await original_reasoning(text, sources)

    monkeypatch.setattr(services, "reasoning", reasoning_after_withdrawal)
    await process_turn(database, ServiceBundle(services, services), session, second)

    saved = database.get_turn(call_id, "second")
    assert saved is not None and saved["status"] == "interrupted"
    assert saved["final_text"] is None and saved["error"] == "location_context_revoked"


@pytest.mark.anyio
async def test_stale_emergency_turn_is_not_replayed_after_location_change(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, 'old-emergency', '숨을 못 쉬겠어요')
    session = database.get_session(call_id)
    assert session is not None
    RecommendationOutbox(database).set_location_consent(call_id, False)
    services = RecordingHistory(database, call_id)

    await process_turn(database, ServiceBundle(services, services), session, turn)

    saved = database.get_turn(call_id, 'old-emergency')
    assert saved is not None and saved['status'] == 'interrupted'
    assert saved['final_text'] is None and not services.prompts


def test_history_admission_hides_legacy_unverified_answers_after_withdrawal(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    ordinary, _ = database.create_turn(call_id, "ordinary", "기침은 어제부터예요")
    database.update_turn(str(ordinary["id"]), {"status": "completed", "final_text": "진료 때 시작 시점을 알려 주세요.",
        "sources_json": '[{"id":"clinic:ordinary","type":"clinic"}]'})
    RecommendationOutbox(database).set_location_consent(call_id, False)

    admitted = conversation_admission.admit_conversation(database,
        conversation_admission.ConversationScope(call_id), database.history(call_id))

    assert admitted.history[0]["input_text"] == ""
    assert admitted.history[0]["final_text"] is None
    assert database.get_turn(call_id, 'ordinary')['input_text'] == '기침은 어제부터예요'


def test_location_revoked_hides_legacy_raw_history_even_after_reacceptance(tmp_path: Path) -> None:
    client, database, _, _ = setup_client(tmp_path)
    with client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        old, _ = database.create_turn(call_id, 'legacy', '화순 동면에서 무릎이 아파요')
        database.update_turn(str(old['id']), {'status': 'completed', 'final_text': '화순 동면이시군요.'})
        withdrawn = client.post(f'/calls/{call_id}/location-consent', json={'accepted': False}).json()
        client.post(f'/calls/{call_id}/location-consent', json={'accepted': True, 'source_location_generation': 0,
                                                                'source_location_revision': withdrawn['location_revision']})
        current, _ = database.create_turn(call_id, 'current', '준비물 알려 주세요')
        admitted = conversation_admission.admit_conversation(database,
            conversation_admission.ConversationScope(call_id, str(current['id'])), database.history(call_id))
    assert '화순 동면' not in json.dumps(admitted.history, ensure_ascii=False)


def test_single_dependency_schema_upgrade_preserves_existing_provenance(tmp_path: Path) -> None:
    database, call_id = prepared(tmp_path)
    turn, _ = database.create_turn(call_id, "old-schema", "병원 전화번호요")
    context = load_recommendation_context(database, call_id)
    assert context is not None and bind_context_turn(database, str(turn["id"]), context)
    with closing(database.connect()) as con, con:
        con.execute("ALTER TABLE recommendation_turn_contexts RENAME TO old_contexts")
        con.execute("""CREATE TABLE recommendation_turn_contexts (
            turn_id TEXT PRIMARY KEY REFERENCES turns(id), event_id TEXT NOT NULL REFERENCES recommendation_outbox(event_id),
            generation INTEGER NOT NULL CHECK(generation > 0))""")
        con.execute("INSERT INTO recommendation_turn_contexts SELECT * FROM old_contexts")
        con.execute("DROP TABLE old_contexts")

    RecommendationOutbox(database).initialize()

    with closing(database.connect()) as con:
        row = con.execute("SELECT event_id,generation FROM recommendation_turn_contexts WHERE turn_id=?", (turn["id"],)).fetchone()
        keys = {column["name"]: column["pk"] for column in con.execute("PRAGMA table_info(recommendation_turn_contexts)")}
    assert row["event_id"] == context.event_id and row["generation"] == context.location_generation
    assert keys["turn_id"] == 1 and keys["event_id"] == 2
