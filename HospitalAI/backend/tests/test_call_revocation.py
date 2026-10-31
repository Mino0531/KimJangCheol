from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from app.call_revocation import revoke_call
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.municipal_bridge import MunicipalOutbox
from app.orchestrator import ServiceBundle, process_turn
from app.recommend_dashboard import RecommendationOutbox
from app.services import MockLLM, MockTTS


def test_revocation_stops_pending_transmission_and_location(tmp_path: Path) -> None:
    path = tmp_path / 'revocation.sqlite3'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='service', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        assert client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'}).json()['state'] == 'accepted'
        outbox = MunicipalOutbox(db)
        assert outbox._pending() == 1
        previous = RecommendationOutbox(db).location_generation(call_id)
        assert previous is not None
        assert revoke_call(db, call_id)
        assert RecommendationOutbox(db).location_generation(call_id) is None
        assert outbox._pending() == 0
        assert outbox._next() is None
        assert client.post(f'/calls/{call_id}/messages', json={'request_id': 'x', 'text': '기침'}).status_code == 403


def test_revocation_preserves_other_calls(tmp_path: Path) -> None:
    path = tmp_path / 'calls.sqlite3'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='service', database_path=str(path)),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        a = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        b = client.post('/calls', json={'phone': '+821000000002'}).json()['id']
        for call_id in (a, b):
            client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        assert revoke_call(db, a)
        assert RecommendationOutbox(db).location_generation(b) is not None
        assert MunicipalOutbox(db)._pending() == 1


@pytest.mark.anyio
async def test_revocation_cancels_inflight_model_and_audio(tmp_path: Path) -> None:
    path = tmp_path / 'inflight.sqlite3'
    db = Database(path)
    app = create_app(Settings(environment='development', service_token='service', database_path=str(path), seed_demo=True),
                     db, ServiceBundle(MockLLM(), MockTTS()))
    with TestClient(app, headers={'Authorization': 'Bearer service'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의합니다'})
        turn, _ = db.create_turn(call_id, 'pending', '기침이 나요')
        session = db.get_session(call_id)
        started = anyio.Event()

        class SlowLLM:
            async def low(self, text: str) -> str:
                await anyio.sleep_forever()
            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                started.set()
                await anyio.sleep_forever()

        class CountingTTS:
            count = 0
            async def deliver(self, text: str, kind: str):
                self.count += 1
                return await MockTTS().deliver(text, kind)

        tts = CountingTTS()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(process_turn, db, ServiceBundle(SlowLLM(), tts), session, turn)
            with anyio.fail_after(5):
                await started.wait()
            assert revoke_call(db, call_id)
        stored = db.get_turn(call_id, 'pending')
        assert stored['status'] == 'cancelled'
        assert stored['final_text'] is None
        assert tts.count == 0
