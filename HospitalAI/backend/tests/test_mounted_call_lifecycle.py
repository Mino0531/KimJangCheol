"""The mounted deployment gateway preserves call lifecycle authorization."""

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.call_capabilities import CallCapabilities
from app import azure_stt
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy' / 'phone'))
from gateway import GatewaySecrets, build_gateway  # noqa: E402


@pytest.mark.parametrize('mounted', [False, True])
def test_ended_call_keeps_end_and_withdrawal_available(tmp_path: Path, mounted: bool) -> None:
    """An ASGI root path must not block idempotent end or consent withdrawal."""
    service = 'gateway-test-service'
    proxy = 'gateway-test-proxy-' + 'x' * 32
    database = Database(tmp_path / 'mounted.sqlite3')
    app = create_app(Settings(environment='development', service_token=service,
                              database_path=database.path),
                     database, ServiceBundle(MockLLM(), MockTTS()))
    prefix = '/api' if mounted else ''
    if mounted:
        capabilities = CallCapabilities(database, service, 'development')
        app = build_gateway(app, capabilities, GatewaySecrets(proxy, service))
    origin = 'http://127.0.0.1:8000'
    with TestClient(app, base_url=origin, client=('127.0.0.1', 12345)) as client:
        def new_call() -> tuple[str, dict[str, str]]:
            headers = {'Origin': origin}
            if mounted:
                headers['X-Hospital-Gateway'] = proxy
            response = client.post(f'{prefix}/calls', json={'phone': '+821000000001'}, headers=headers)
            assert response.status_code == 201
            payload = response.json()
            return payload['id'], {'Authorization': f"Bearer {payload['call_token']}"}

        call_id, auth = new_call()
        assert client.post(f'{prefix}/calls/{call_id}/consent', json={'text': '동의합니다'}, headers=auth).status_code == 200
        end = f'{prefix}/calls/{call_id}/end'
        assert client.post(end, json={'reason': 'caller'}, headers=auth).status_code == 200
        assert client.post(end, json={'reason': 'caller'}, headers=auth).status_code == 200
        assert client.post(f'{prefix}/calls/{call_id}/messages',
                           json={'request_id': 'late', 'text': '다시 말씀드립니다'}, headers=auth).status_code == 409
        assert client.post(f'{prefix}/calls/{call_id}/revoke', headers=auth).status_code == 200

        location_call, location_auth = new_call()
        assert client.post(f'{prefix}/calls/{location_call}/consent', json={'text': '동의합니다'}, headers=location_auth).status_code == 200
        assert client.post(f'{prefix}/calls/{location_call}/end', json={'reason': 'caller'}, headers=location_auth).status_code == 200
        result = client.post(f'{prefix}/calls/{location_call}/location-consent',
                             json={'accepted': False}, headers=location_auth)
        assert result.status_code == 200
        assert result.json()['state'] == 'revoked'


def test_mounted_websocket_checks_origin_and_call_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment prefix preserves speech origin and first-frame authentication."""
    class Recognizer:
        def __init__(self, _key: str, _region: str, _events: azure_stt.SpeechEvents) -> None:
            pass

        def start(self) -> None:
            pass

        def write(self, _data: bytes) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', Recognizer)
    service = 'gateway-test-service'
    proxy = 'gateway-test-proxy-' + 'x' * 32
    database = Database(tmp_path / 'speech.sqlite3')
    settings = Settings(environment='development', service_token=service, database_path=database.path,
                        azure_speech_key='fake', azure_speech_region='fake')
    phone = create_app(settings, database, ServiceBundle(MockLLM(), MockTTS()))
    app = build_gateway(phone, CallCapabilities(database, service, 'development'), GatewaySecrets(proxy, service))
    origin = 'https://demo.example'
    with TestClient(app, base_url=origin, client=('127.0.0.1', 12345)) as client:
        response = client.post('/api/calls', json={'phone': '+821000000001'},
                               headers={'Origin': origin, 'X-Hospital-Gateway': proxy})
        assert response.status_code == 201
        call = response.json()
        path = f"wss://demo.example/api/calls/{call['id']}/speech"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={'Origin': 'https://invalid.example'}) as socket:
                socket.receive_json()
        with client.websocket_connect(path, headers={'Origin': origin}) as socket:
            socket.send_json({'type': 'start', 'token': 'invalid', 'stt_notice_acknowledged': True})
            assert socket.receive_json()['code'] == 'invalid_start'
        with client.websocket_connect(path, headers={'Origin': origin}) as socket:
            socket.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert socket.receive_json()['type'] == 'ready'
