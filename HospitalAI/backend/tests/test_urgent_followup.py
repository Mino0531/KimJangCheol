"""Urgent calls retain a bounded safety conversation without routine advice."""

import base64
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor

import anyio
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS, TTSResult


@dataclass(frozen=True)
class Config:
    low_model: str = 'test-low'
    allow_medical_data: bool = True


class LogisticsModel(MockLLM):
    config = Config()

    def __init__(self, decision: str = 'continue') -> None:
        self.decision = decision
        self.payloads: list[str] = []

    async def _complete(self, model: str, system: str, user: str) -> str:
        self.payloads.append(user)
        return '{"intent":"' + self.decision + '"}'


class AudioTTS(MockTTS):
    async def deliver(self, text: str, kind: str) -> TTSResult:
        return TTSResult('synthesized', kind, text, False, b'test audio', 'audio/mpeg')


def app_client(tmp_path: Path, model: LogisticsModel | None = None, tts: MockTTS | None = None) -> tuple[TestClient, Database]:
    path = tmp_path / 'urgent-followup.sqlite'
    db = Database(path)
    settings = Settings(environment='development', service_token='test', database_path=str(path))
    client = TestClient(create_app(settings, db, ServiceBundle(model or LogisticsModel(), tts or AudioTTS())),
                        headers={'Authorization': 'Bearer test'})
    return client, db


def start_call(client: TestClient, accepted: bool = True, urgent: bool = True) -> str:
    call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
    if accepted:
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의'}).status_code == 200
    if urgent:
        assert client.post(f'/calls/{call_id}/urgent').status_code == 200
    return call_id


def test_urgent_followup_stays_open_and_routine_processing_stays_blocked(tmp_path: Path) -> None:
    client, db = app_client(tmp_path)
    with client:
        call_id = start_call(client)
        reply = client.post(f'/calls/{call_id}/urgent-followup', json={'text': '옆에 아무도 없어요'})
        assert reply.status_code == 200
        assert reply.json()['action'] == 'continue'
        assert '119' in reply.json()['prompt']
        assert client.post(f'/calls/{call_id}/interpret', json={'text': '병원 알려줘'}).status_code == 409
        assert client.post(f'/calls/{call_id}/messages', json={'request_id': 'routine', 'text': '괜찮아졌어요'}).status_code == 409
        with db.connect() as con:
            assert con.execute('SELECT urgent,state FROM calls WHERE id=?', (call_id,)).fetchone()[:] == (1, 'accepted')


@pytest.mark.parametrize('accepted,urgent,code', [(False, True, 403), (True, False, 409)])
def test_followup_rejects_without_consent_or_latch(tmp_path: Path, accepted: bool, urgent: bool, code: int) -> None:
    model = LogisticsModel()
    client, db = app_client(tmp_path, model)
    with client:
        call_id = start_call(client, accepted, urgent)
        assert client.post(f'/calls/{call_id}/urgent-followup', json={'text': '민감한 내용'}).status_code == code
        assert model.payloads == []
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM urgent_followups').fetchone()[0] == 0


def test_bare_yes_does_not_claim_119_contacted(tmp_path: Path) -> None:
    client, _ = app_client(tmp_path, LogisticsModel('contacted119'))
    with client:
        call_id = start_call(client)
        reply = client.post(f'/calls/{call_id}/urgent-followup', json={'text': '네'}).json()
        assert reply['action'] == 'continue'
        assert reply['prompt_kind'] == 'urgent_help'


@pytest.mark.parametrize('intent,action,kind', [
    ('contacted119', 'continue', 'urgent_connected'),
    ('cannot_call', 'continue', 'urgent_support'),
    ('nearby_help', 'continue', 'urgent_nearby'),
    ('end', 'end', 'urgent_goodbye'),
    ('revoke', 'revoke', 'urgent_revoked'),
])
def test_bounded_actions_and_matching_audio(tmp_path: Path, intent: str, action: str, kind: str) -> None:
    client, _ = app_client(tmp_path, LogisticsModel(intent))
    with client:
        call_id = start_call(client)
        reply = client.post(f'/calls/{call_id}/urgent-followup', json={'text': '지금 요청합니다'}).json()
        assert reply['action'] == action
        assert reply['prompt_kind'] == kind
        audio = client.get(f'/calls/{call_id}/urgent-followup/audio/{kind}')
        if action == 'end':
            assert audio.status_code == 409
            assert client.get(f'/calls/{call_id}').json()['lifecycle'] == 'ended'
            return
        assert audio.status_code == 200
        assert base64.b64decode(audio.headers['X-Spoken-Text-Base64']).decode() == reply['prompt']
        if action == 'revoke':
            assert client.post(f'/calls/{call_id}/urgent-followup', json={'text': '다시'}).status_code == 403


def test_unknown_model_output_falls_back_and_request_retry_is_idempotent(tmp_path: Path) -> None:
    model = LogisticsModel('diagnose')
    client, db = app_client(tmp_path, model)
    with client:
        call_id = start_call(client)
        body = {'request_id': 'same', 'text': '괜찮아졌어'}
        first = client.post(f'/calls/{call_id}/urgent-followup', json=body)
        second = client.post(f'/calls/{call_id}/urgent-followup', json=body)
        assert first.json() == second.json()
        assert first.json()['prompt_kind'] == 'urgent_help'
        assert len(model.payloads) == 1
        assert client.post(f'/calls/{call_id}/urgent-followup', json={**body, 'text': '다른 말'}).status_code == 409
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM urgent_followups WHERE call_id=?', (call_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('control,status', [('revoke', 403), ('end', 409)])
def test_control_during_model_request_discards_late_result(tmp_path: Path, control: str, status: int) -> None:
    entered, release = Event(), Event()

    class WaitingModel(LogisticsModel):
        async def _complete(self, model: str, system: str, user: str) -> str:
            entered.set()
            await anyio.to_thread.run_sync(release.wait)
            return '{"intent":"contacted119"}'

    client, db = app_client(tmp_path, WaitingModel())
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        future = pool.submit(client.post, f'/calls/{call_id}/urgent-followup', json={'text': '119 전화했어요'})
        assert entered.wait(3)
        body = {'reason': 'caller'} if control == 'end' else {}
        assert client.post(f'/calls/{call_id}/{control}', json=body).status_code == 200
        release.set()
        assert future.result(timeout=5).status_code == status
        with db.connect() as con:
            assert con.execute('SELECT COUNT(*) FROM urgent_followups').fetchone()[0] == 0


@pytest.mark.parametrize('control,status', [('revoke', 403), ('end', 409)])
def test_control_during_tts_discards_late_audio(tmp_path: Path, control: str, status: int) -> None:
    entered, release = Event(), Event()

    class WaitingTTS(AudioTTS):
        async def deliver(self, text: str, kind: str) -> TTSResult:
            entered.set()
            await anyio.to_thread.run_sync(release.wait)
            return await super().deliver(text, kind)

    client, _ = app_client(tmp_path, tts=WaitingTTS())
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        future = pool.submit(client.get, f'/calls/{call_id}/urgent-followup/audio/urgent_help')
        assert entered.wait(3)
        body = {'reason': 'caller'} if control == 'end' else {}
        assert client.post(f'/calls/{call_id}/{control}', json=body).status_code == 200
        release.set()
        assert future.result(timeout=5).status_code == status


def test_logistics_context_is_same_call_only(tmp_path: Path) -> None:
    import json

    model = LogisticsModel()
    client, _ = app_client(tmp_path, model)
    with client:
        first, second = start_call(client), start_call(client)
        client.post(f'/calls/{first}/urgent-followup', json={'text': '혼자 있어요'})
        client.post(f'/calls/{first}/urgent-followup', json={'text': '옆집에 도움을 청할게요'})
        assert json.loads(model.payloads[1])['previous_turns'][0]['caller'] == '혼자 있어요'
        client.post(f'/calls/{second}/urgent-followup', json={'text': '다른 통화예요'})
        assert json.loads(model.payloads[2])['previous_turns'] == []


def test_external_model_setting_required_but_fixed_help_remains_available(tmp_path: Path) -> None:
    model = LogisticsModel('end')
    model.config = Config(allow_medical_data=False)
    client, _ = app_client(tmp_path, model)
    with client:
        call_id = start_call(client)
        reply = client.post(f'/calls/{call_id}/urgent-followup', json={'text': '괜찮아진 것 같아요'}).json()
        assert reply['action'] == 'continue'
        assert reply['prompt_kind'] == 'urgent_help'
        assert model.payloads == []


def test_concurrent_duplicate_id_does_not_store_rejected_caller_text(tmp_path: Path) -> None:
    barrier = Barrier(2)

    class ParallelModel(LogisticsModel):
        async def _complete(self, model: str, system: str, user: str) -> str:
            await anyio.to_thread.run_sync(barrier.wait)
            return '{"intent":"continue"}'

    client, db = app_client(tmp_path, ParallelModel())
    with client, ThreadPoolExecutor(max_workers=2) as pool:
        call_id = start_call(client)
        replies = [pool.submit(client.post, f'/calls/{call_id}/urgent-followup',
                               json={'request_id': 'duplicate', 'text': text})
                   for text in ('혼자 있어요', '이웃이 왔어요')]
        assert sorted(reply.result(timeout=10).status_code for reply in replies) == [200, 409]
        with db.connect() as con:
            saved = con.execute('SELECT input_text FROM urgent_followups WHERE call_id=?', (call_id,)).fetchall()
            details = con.execute('SELECT text FROM urgent_detail_events WHERE call_id=?', (call_id,)).fetchall()
        assert [row[0] for row in details] == [row[0] for row in saved]
