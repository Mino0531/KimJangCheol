"""Interruption prevents unheard responses from leaking into subsequent care."""

from pathlib import Path
import json
import sqlite3

import pytest
import anyio
from fastapi.testclient import TestClient

from app.audio_store import Audio, AudioStore
from app.call_store import CallStore
from app.config import Settings
from app.conversation_context import build_conversation_context
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle, process_turn
from app.services import MockLLM, MockTTS, WAIT_MESSAGE


@pytest.fixture
def calls(tmp_path: Path):
    database = Database(tmp_path / "interrupt.sqlite3")
    settings = Settings(environment="development", service_token="staff", database_path=str(database.path))
    app = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234)) as client:
        yield client, database


def _new_call(client: TestClient) -> tuple[str, dict[str, str]]:
    response = client.post("/calls", json={"phone": "+821000000001"},
                           headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 201
    call = response.json()
    headers = {"Authorization": f"Bearer {call['call_token']}"}
    assert client.post(f"/calls/{call['id']}/consent", json={"text": "동의"}, headers=headers).status_code == 200
    return call["id"], headers


def _turn(database: Database, call_id: str, status: str = "processing") -> dict:
    turn, _ = database.create_turn(call_id, "question-1", "며칠 전부터 기침이 나요")
    database.update_turn(str(turn["id"]), {
        "status": status, "initial_text": "잠시만요", "final_text": "언제부터인가요?",
        "sources_json": "[]", "tts_json": "{}",
    })
    AudioStore(database).save(str(turn["id"]), "final", Audio(b"sound", "audio/mpeg"))
    return turn


@pytest.mark.parametrize("status", ["pending", "processing", "completed", "degraded"])
def test_interrupt_prevents_unheard_output_and_late_writes(calls, status: str) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, status)
    request_id = "question-1"

    response = client.post(f"/calls/{call_id}/messages/{request_id}/interrupt", json={}, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "interrupted"
    saved = database.get_turn(call_id, request_id)
    assert saved is not None and saved["status"] == "interrupted"
    assert saved["input_text"] == "며칠 전부터 기침이 나요"
    assert saved["final_text"] is None
    assert AudioStore(database).get(str(turn["id"]), "final") is None
    assert not CallStore(database).update_turn_if_safe(str(turn["id"]), {"status": "completed", "final_text": "late"})
    assert not CallStore(database).save_audio_if_safe(str(turn["id"]), "final", Audio(b"late", "audio/mpeg"))


def test_interruption_keeps_caller_symptom_but_omits_unheard_assistant_text(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    _turn(database, call_id, "completed")
    assert client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers).status_code == 200
    next_turn, _ = database.create_turn(call_id, "question-2", "어제부터요")

    context = build_conversation_context(database.history(call_id), next_turn)

    assert "며칠 전부터 기침이 나요" in context.search_query
    assert "언제부터인가요?" not in context.prompt_text


def test_interrupt_prevents_queued_municipal_delivery(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, "completed")
    outbox = MunicipalOutbox(database)
    assert outbox.enqueue_turn(call_id, "question-1")
    event_id = f"turn:{turn['id']}"

    response = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)

    assert response.status_code == 200
    assert response.json()["delivery_may_have_occurred"] is True
    assert not outbox._eligible(event_id, call_id)
    assert not outbox.enqueue_turn(call_id, "question-1")
    reopened = MunicipalOutbox(Database(database.path))
    reopened.initialize()
    reopened.recover()
    assert not reopened._eligible(event_id, call_id)


def test_service_failure_interruption_preserves_distinct_reason_for_staff(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id)
    response = client.post(f"/calls/{call_id}/messages/question-1/interrupt",
                           json={"reason": "service_error"}, headers=headers)
    assert response.status_code == 200
    saved = database.get_turn(call_id, "question-1")
    assert saved is not None and saved["error"] == "service_error"
    with database.connect() as con:
        payload = json.loads(con.execute("SELECT body FROM municipal_outbox WHERE event_id=?",
                                        (f"interrupted:{turn['id']}",)).fetchone()[0])
    assert "연결 오류" in payload["assistant_text"]
    assert payload["user_text"] == "며칠 전부터 기침이 나요"
    assert payload["answer_status"] == "interrupted"


def test_interrupted_event_is_durable_idempotent_and_replaces_pending_answer(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, "completed")
    outbox = MunicipalOutbox(database)
    assert outbox.enqueue_turn(call_id, "question-1")
    old_event = f"turn:{turn['id']}"
    interrupted_event = f"interrupted:{turn['id']}"

    first = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)
    second = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)
    reopened = MunicipalOutbox(Database(database.path))
    reopened.initialize()
    reopened.recover()

    assert first.status_code == second.status_code == 200
    assert not reopened._eligible(old_event, call_id)
    assert reopened._eligible(interrupted_event, call_id)
    with database.connect() as con:
        rows = con.execute("SELECT body FROM municipal_outbox WHERE event_id=?", (interrupted_event,)).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["event_type"] == "turn_interrupted"
    assert payload["answer_status"] == "interrupted"
    assert payload["user_text"] == "며칠 전부터 기침이 나요"
    assert payload["assistant_text"] != "언제부터인가요?"
    assert payload["sources"] == []


def test_failed_interruption_event_rolls_back_turn_and_pending_delivery(calls, monkeypatch: pytest.MonkeyPatch) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, "completed")
    outbox = MunicipalOutbox(database)
    assert outbox.enqueue_turn(call_id, "question-1")
    event_id = f"turn:{turn['id']}"

    def fail_insert(*args, **kwargs):
        raise sqlite3.OperationalError("test interruption queue failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(MunicipalOutbox, "insert_interrupted", fail_insert)
        with pytest.raises(sqlite3.OperationalError):
            client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)

    saved = database.get_turn(call_id, "question-1")
    assert saved is not None and saved["status"] == "completed"
    assert saved["final_text"] == "언제부터인가요?"
    assert AudioStore(database).get(str(turn["id"]), "final") is not None
    assert outbox._eligible(event_id, call_id)


def test_interruption_reports_when_nothing_was_queued(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    _turn(database, call_id)

    response = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)

    assert response.status_code == 200
    assert response.json()["delivery_may_have_occurred"] is False


def test_interruption_does_not_claim_to_recall_delivered_records(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, "completed")
    outbox = MunicipalOutbox(database)
    assert outbox.enqueue_turn(call_id, "question-1")
    event_id = f"turn:{turn['id']}"
    with database.connect() as con:
        con.execute("UPDATE municipal_outbox SET state='sent' WHERE event_id=?", (event_id,))
        con.commit()

    response = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)

    assert response.status_code == 200
    assert response.json()["delivery_may_have_occurred"] is True
    with database.connect() as con:
        assert con.execute("SELECT state FROM municipal_outbox WHERE event_id=?", (event_id,)).fetchone()[0] == "sent"


def test_interrupt_is_idempotent_and_isolated_by_call_token(calls) -> None:
    client, database = calls
    first, first_headers = _new_call(client)
    second, second_headers = _new_call(client)
    _turn(database, first)
    path = f"/calls/{first}/messages/question-1/interrupt"

    assert client.post(path, json={}, headers=second_headers).status_code in (401, 403, 404)
    assert client.post(path, json={}, headers=first_headers).status_code == 200
    assert client.post(path, json={}, headers=first_headers).json()["status"] == "interrupted"
    assert database.get_turn(second, "question-1") is None


def test_emergency_cannot_be_replaced_by_interruption(calls) -> None:
    client, database = calls
    call_id, headers = _new_call(client)
    turn = _turn(database, call_id, "emergency")

    response = client.post(f"/calls/{call_id}/messages/question-1/interrupt", json={}, headers=headers)

    assert response.status_code == 409
    assert database.get_turn(call_id, "question-1")["status"] == "emergency"
    assert AudioStore(database).get(str(turn["id"]), "final") is not None


def test_interrupt_cancels_inflight_reasoning_and_suppresses_late_completion(calls) -> None:
    client, database = calls
    database.initialize(seed=True)
    call_id, headers = _new_call(client)
    turn, _ = database.create_turn(call_id, "inflight", "기침이 나요")
    session = database.get_session(call_id)
    assert session is not None

    async def scenario() -> None:
        started, release, done = anyio.Event(), anyio.Event(), anyio.Event()

        class DelayedLLM:
            async def low(self, text: str) -> str:
                return WAIT_MESSAGE

            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                started.set()
                # A downstream adapter may complete despite cancellation.
                with anyio.CancelScope(shield=True):
                    await release.wait()
                return "늦게 도착한 이전 답변"

        async def run_turn() -> None:
            await process_turn(database, ServiceBundle(DelayedLLM(), MockTTS()), session, turn)
            done.set()

        async with anyio.create_task_group() as group:
            group.start_soon(run_turn)
            with anyio.fail_after(5):
                await started.wait()
                response = await anyio.to_thread.run_sync(
                    lambda: client.post(f"/calls/{call_id}/messages/inflight/interrupt", json={}, headers=headers)
                )
                assert response.status_code == 200
                release.set()
                await done.wait()

    anyio.run(scenario)
    saved = database.get_turn(call_id, "inflight")
    assert saved is not None and saved["status"] == "interrupted"
    assert saved["final_text"] is None
    assert AudioStore(database).get(str(turn["id"]), "final") is None
    assert not MunicipalOutbox(database).enqueue_turn(call_id, "inflight")
