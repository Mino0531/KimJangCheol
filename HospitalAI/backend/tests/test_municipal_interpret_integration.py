"""Exercise delivery gating while a caller's intent is being interpreted."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.call_store import CallStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS, SplitLLM


class BlockingLow:
    class config:
        low_model = 'fake'
        allow_medical_data = True

    def __init__(self, result: str | None):
        self.entered = Event()
        self.release = Event()
        self.result = result

    async def _complete(self, model, system, user):
        import anyio

        self.entered.set()
        await anyio.to_thread.run_sync(self.release.wait)
        if self.result is None:
            raise RuntimeError('intent provider failed')
        if self.result == 'timeout':
            raise TimeoutError('intent provider timed out')
        return self.result


def fixture_client(path: Path, provider: BlockingLow):
    settings = Settings(environment='development', service_token='service', database_path=str(path))
    db = Database(settings.database_path)
    app = create_app(settings, db, ServiceBundle(SplitLLM(provider, MockLLM()), MockTTS()))
    return TestClient(app, headers={'Authorization': 'Bearer service'}, raise_server_exceptions=False), db


def start_call(client: TestClient) -> str:
    call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
    assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).status_code == 200
    return call_id


def test_inflight_caller_intent_blocks_delivery_until_answer(tmp_path: Path):
    provider = BlockingLow('{"intent":"consultation"}')
    client, database = fixture_client(tmp_path / 'intent.sqlite', provider)
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        outbox = MunicipalOutbox(database)
        future = pool.submit(client.post, f'/calls/{call_id}/interpret', json={'text': '며칠 전부터 아파요'})
        try:
            assert provider.entered.wait(5)
            assert not outbox._eligible(f'start:{call_id}', call_id)
        finally:
            provider.release.set()
        assert future.result(timeout=5).json()['intent'] == 'consultation'
        assert outbox._eligible(f'start:{call_id}', call_id)


def test_consent_revocation_finishes_before_delivery_gate_opens(tmp_path: Path):
    provider = BlockingLow('{"intent":"revoke_consent"}')
    client, database = fixture_client(tmp_path / 'revoke.sqlite', provider)
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        outbox = MunicipalOutbox(database)
        future = pool.submit(client.post, f'/calls/{call_id}/interpret', json={'text': '제 정보는 더 이상 쓰지 말아 주세요'})
        try:
            assert provider.entered.wait(5)
            assert not outbox._eligible(f'start:{call_id}', call_id)
        finally:
            provider.release.set()
        assert future.result(timeout=5).json()['intent'] == 'revoke_consent'
        assert not outbox._eligible(f'start:{call_id}', call_id)


def test_delivery_gate_is_released_when_provider_raises(tmp_path: Path):
    provider = BlockingLow(None)
    client, database = fixture_client(tmp_path / 'failure.sqlite', provider)
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        outbox = MunicipalOutbox(database)
        future = pool.submit(client.post, f'/calls/{call_id}/interpret', json={'text': '무슨 말인지 모르겠어요'})
        try:
            assert provider.entered.wait(5)
            assert not outbox._eligible(f'start:{call_id}', call_id)
        finally:
            provider.release.set()
        assert future.result(timeout=5).status_code == 500
        assert outbox._eligible(f'start:{call_id}', call_id)


def test_delivery_gate_is_released_when_interpretation_times_out(tmp_path: Path):
    provider = BlockingLow('timeout')
    client, database = fixture_client(tmp_path / 'timeout.sqlite', provider)
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        outbox = MunicipalOutbox(database)
        future = pool.submit(client.post, f'/calls/{call_id}/interpret', json={'text': '무슨 말인지 모르겠어요'})
        try:
            assert provider.entered.wait(5)
            assert not outbox._eligible(f'start:{call_id}', call_id)
        finally:
            provider.release.set()
        assert future.result(timeout=5).json()['intent'] == 'uncertain'
        assert outbox._eligible(f'start:{call_id}', call_id)


def test_urgent_notification_bypasses_active_interpretation_gate(tmp_path: Path):
    provider = BlockingLow('{"intent":"consultation"}')
    client, database = fixture_client(tmp_path / 'urgent.sqlite', provider)
    with client, ThreadPoolExecutor(max_workers=1) as pool:
        call_id = start_call(client)
        outbox = MunicipalOutbox(database)
        future = pool.submit(client.post, f'/calls/{call_id}/interpret', json={'text': '배가 아파요'})
        try:
            assert provider.entered.wait(5)
            assert not outbox._eligible(f'start:{call_id}', call_id)
            assert client.post(f'/calls/{call_id}/urgent', json={}).status_code == 200
            assert outbox._eligible(f'urgent:{call_id}', call_id)
        finally:
            provider.release.set()
        assert future.result(timeout=5).status_code == 409


@pytest.mark.anyio
async def test_cancelled_interpretation_releases_delivery_gate(tmp_path: Path):
    entered = anyio.Event()

    class CancelledLow:
        class config:
            low_model = 'fake'
            allow_medical_data = True

        async def _complete(self, model, system, user):
            entered.set()
            await anyio.sleep_forever()

    settings = Settings(environment='development', service_token='service', database_path=str(tmp_path / 'cancel.sqlite'))
    database = Database(settings.database_path)
    app = create_app(settings, database, ServiceBundle(SplitLLM(CancelledLow(), MockLLM()), MockTTS()))
    store = CallStore(database)
    outbox = MunicipalOutbox(database)
    call_id = str(database.create_session('+821000000001', None)['id'])
    store.create(call_id)
    assert store.decide(call_id, 'accepted')
    outbox.enqueue_start(call_id)
    scopes = []

    async def request(client):
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await client.post(f'/calls/{call_id}/interpret', json={'text': '아파요'})

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://test',
                                  headers={'Authorization': 'Bearer service'}) as client:
        async with anyio.create_task_group() as group:
            group.start_soon(request, client)
            with anyio.fail_after(5):
                await entered.wait()
            assert not outbox._eligible(f'start:{call_id}', call_id)
            scopes[0].cancel()

    assert outbox._eligible(f'start:{call_id}', call_id)
