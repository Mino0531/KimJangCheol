"""Speech transport accepts only an authorized, disclosed call stream."""

from pathlib import Path
from queue import Queue

from fastapi.testclient import TestClient
import anyio
from starlette.websockets import WebSocketDisconnect
import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.services import MockLLM, MockTTS


def make_client(tmp_path: Path, configured: bool = True) -> TestClient:
    path = tmp_path / "speech.sqlite"
    settings = Settings(environment="development", database_path=str(path),
                        azure_speech_key="test" if configured else "", azure_speech_region="test" if configured else "")
    return TestClient(create_app(settings, Database(path), ServiceBundle(MockLLM(), MockTTS())),
                      base_url="http://127.0.0.1:8000", client=("127.0.0.1", 1234))


def new_call(client: TestClient) -> dict:
    return client.post("/calls", json={"phone": "+821000000001"},
                       headers={"Origin": "http://127.0.0.1:8000"}).json()


def test_recognizer_allows_longer_pauses_in_a_single_utterance(monkeypatch) -> None:
    import azure.cognitiveservices.speech as sdk
    from app.azure_stt import AzureRecognizer, SpeechEvents

    captured = {}

    class Signal:
        def connect(self, callback):
            pass

    class FakeRecognizer:
        def __init__(self, speech_config, audio_config):
            captured['config'] = speech_config
            self.recognizing = Signal()
            self.recognized = Signal()
            self.canceled = Signal()

    monkeypatch.setattr(sdk, 'SpeechRecognizer', FakeRecognizer)
    AzureRecognizer('test', 'test', SpeechEvents())
    assert captured['config'].get_property(sdk.PropertyId.Speech_SegmentationSilenceTimeoutMs) == '1200'
    assert captured['config'].get_property(sdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs) == '0'


@pytest.mark.parametrize('sdk_reason, expected', [('InitialSilenceTimeout', 'silence'), ('NotRecognized', 'unrecognized')])
def test_recognizer_distinguishes_silence_from_unrecognized_speech(monkeypatch, sdk_reason: str, expected: str) -> None:
    import azure.cognitiveservices.speech as sdk
    from types import SimpleNamespace
    from app.azure_stt import AzureRecognizer, SpeechEvents

    captured = {}

    class Signal:
        def connect(self, callback): self.callback = callback

    class FakeRecognizer:
        def __init__(self, speech_config, audio_config):
            self.recognizing = Signal()
            self.recognized = Signal()
            self.canceled = Signal()
            captured['recognizer'] = self

    monkeypatch.setattr(sdk, 'SpeechRecognizer', FakeRecognizer)
    events = SpeechEvents()
    AzureRecognizer('test', 'test', events)
    result = SimpleNamespace(reason=sdk.ResultReason.NoMatch, text='',
                             no_match_details=SimpleNamespace(reason=getattr(sdk.NoMatchReason, sdk_reason)))
    captured['recognizer'].recognized.callback(SimpleNamespace(result=result))
    assert events.get_nowait() == ('no_match', expected, 1)


def test_no_match_reason_reaches_speech_client(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    class FakeRecognizer:
        def __init__(self, key, region, events): self.events = events
        def start(self): pass
        def close(self): pass
        def write(self, data): self.events.emit_no_match(1, 'silence')

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', FakeRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json() == {'type': 'ready'}
            ws.send_bytes(b'\x00\x00' * 160)
            assert ws.receive_json() == {'type': 'no_match', 'segment': 1, 'reason': 'silence'}


def test_audio_stream_does_not_close_at_a_lifetime_byte_limit(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    class FakeRecognizer:
        def __init__(self, key, region, events): self.events = events
        def start(self): pass
        def close(self): pass
        def write(self, data): self.events.put(('transcript', '계속'))

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', FakeRecognizer)
    monkeypatch.setattr(azure_stt, 'MAX_AUDIO', 640, raising=False)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json() == {'type': 'ready'}
            for _ in range(3):
                ws.send_bytes(b'\x00\x00' * 160)
                assert ws.receive_json() == {'type': 'transcript', 'text': '계속'}


def test_audio_stream_rejects_excessive_burst(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    class FakeRecognizer:
        def __init__(self, key, region, events): self.events = events
        def start(self): pass
        def close(self): pass
        def write(self, data): self.events.put(('transcript', 'frame'))

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', FakeRecognizer)
    monkeypatch.setattr(azure_stt, 'AUDIO_BURST_BYTES', 640)
    monkeypatch.setattr(azure_stt, 'AUDIO_BYTES_PER_SECOND', 0)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json() == {'type': 'ready'}
            for _ in range(2):
                ws.send_bytes(b'\x00\x00' * 160)
                assert ws.receive_json() == {'type': 'transcript', 'text': 'frame'}
            ws.send_bytes(b'\x00\x00' * 160)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1009


def test_missing_configuration_fails_closed(tmp_path: Path) -> None:
    with make_client(tmp_path, False) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
            ws.send_json({"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True})
            assert ws.receive_json() == {"type": "error", "code": "speech_unavailable"}


def test_other_call_token_and_missing_notice_are_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        a = new_call(client)
        b = new_call(client)
        for start in ({"type": "start", "token": b["call_token"], "stt_notice_acknowledged": True},
                      {"type": "start", "token": a["call_token"]}):
            with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{a['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
                ws.send_json(start)
                assert ws.receive_json()["type"] == "error"


def test_audio_is_transcribed_and_released_on_disconnect(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    instances = []
    class FakeRecognizer:
        def __init__(self, key: str, region: str, events: Queue) -> None:
            self.events = events
            self.closed = False
            instances.append(self)

        def start(self) -> None:
            pass

        def write(self, data: bytes) -> None:
            self.events.put(("transcript", "동의"))

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(azure_stt, "AzureRecognizer", FakeRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
            ws.send_json({"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True})
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(b"\x00\x00" * 320)
            assert ws.receive_json() == {"type": "transcript", "text": "동의"}
        assert instances[0].closed


def test_cross_origin_connection_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call = new_call(client)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech",
                                          headers={"Origin": "https://attacker.example"}):
                pass


def test_disconnect_before_start_does_not_reply_to_closed_socket(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech",
                                      headers={"Origin": "http://127.0.0.1:8000"}):
            pass


def test_provider_error_closes_and_releases_stream(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    instances = []
    class FailingRecognizer:
        def __init__(self, key: str, region: str, events: Queue) -> None:
            self.events = events
            self.closed = False
            instances.append(self)

        def start(self) -> None:
            pass

        def write(self, data: bytes) -> None:
            self.events.put(("error", "speech_provider_failed"))

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(azure_stt, "AzureRecognizer", FailingRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
            ws.send_json({"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True})
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(b"\x00\x00" * 320)
            assert ws.receive_json() == {"type": "error", "code": "speech_provider_failed"}
        assert instances[0].closed


def test_second_stream_for_same_call_is_rejected(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt
    class FakeRecognizer:
        def __init__(self, key, region, events): pass
        def start(self): pass
        def close(self): pass
    monkeypatch.setattr(azure_stt, "AzureRecognizer", FakeRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        url = f"ws://127.0.0.1:8000/calls/{call['id']}/speech"
        headers = {"Origin": "http://127.0.0.1:8000"}
        start = {"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True}
        with client.websocket_connect(url, headers=headers) as first:
            first.send_json(start)
            assert first.receive_json() == {"type": "ready"}
            with client.websocket_connect(url, headers=headers) as second:
                second.send_json(start)
                assert second.receive_json() == {"type": "error", "code": "speech_already_active"}
        with client.websocket_connect(url, headers=headers) as again:
            again.send_json(start)
            assert again.receive_json() == {"type": "ready"}


@pytest.mark.parametrize("path,body", [("consent", {"text": "동의하지 않습니다"}), ("urgent", None)])
def test_refused_or_preconsent_urgent_call_cannot_start_speech(tmp_path: Path, path: str, body: dict | None) -> None:
    with make_client(tmp_path) as client:
        call = new_call(client)
        response = client.post(f"/calls/{call['id']}/{path}", json=body,
                               headers={"Authorization": f"Bearer {call['call_token']}"})
        assert response.status_code == 200
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
            ws.send_json({"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True})
            assert ws.receive_json() == {"type": "error", "code": "call_inactive"}


def test_callback_overflow_reports_error_and_closes(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt
    instances = []
    class OverflowRecognizer:
        def __init__(self, key, region, events):
            self.events = events
            self.closed = False
            instances.append(self)
        def start(self): pass
        def write(self, data):
            for _ in range(40):
                self.events.emit(("transcript", "text"))
        def close(self): self.closed = True
    monkeypatch.setattr(azure_stt, "AzureRecognizer", OverflowRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={"Origin": "http://127.0.0.1:8000"}) as ws:
            ws.send_json({"type": "start", "token": call["call_token"], "stt_notice_acknowledged": True})
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(b"\x00\x00" * 320)
            while True:
                result = ws.receive_json()
                if result["type"] == "error":
                    assert result["code"] == "speech_overflow"
                    break
        assert instances[0].closed


def test_recognizer_is_closed_when_ready_send_fails(monkeypatch) -> None:
    from app import azure_stt
    instances = []
    class FakeRecognizer:
        def __init__(self, key, region, events):
            self.closed = False
            instances.append(self)
        def start(self): pass
        def close(self): self.closed = True
    class DisconnectedSocket:
        async def send_json(self, payload):
            raise WebSocketDisconnect()
    monkeypatch.setattr(azure_stt, "AzureRecognizer", FakeRecognizer)
    with pytest.raises(WebSocketDisconnect):
        anyio.run(azure_stt.recognition_session, DisconnectedSocket(),
                  Settings(azure_speech_key="test", azure_speech_region="test"))
    assert instances[0].closed


def test_active_speech_stops_when_consent_is_revoked(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt
    instances = []
    class FakeRecognizer:
        def __init__(self, key, region, events):
            self.closed = False
            self.events = events
            self.frames = []
            instances.append(self)
        def start(self): pass
        def write(self, data):
            self.frames.append(data)
            self.events.put(('transcript', '준비'))
        def close(self): self.closed = True
    monkeypatch.setattr(azure_stt, 'AzureRecognizer', FakeRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        headers = {'Authorization': f"Bearer {call['call_token']}"}
        assert client.post(f"/calls/{call['id']}/consent", json={'text': '동의'}, headers=headers).status_code == 200
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json() == {'type': 'ready'}
            ws.send_bytes(b'\x00\x00' * 320)
            assert ws.receive_json() == {'type': 'transcript', 'text': '준비'}
            assert client.post(f"/calls/{call['id']}/revoke", json={}, headers=headers).status_code == 200
            assert ws.receive_json() == {'type': 'error', 'code': 'call_inactive'}
        assert instances[0].closed


@pytest.mark.parametrize('already_urgent', [False, True])
def test_consented_urgent_speech_starts_and_continues(tmp_path: Path, monkeypatch, already_urgent: bool) -> None:
    from app import azure_stt

    instances = []

    class FakeRecognizer:
        def __init__(self, key, region, events):
            self.events = events
            self.closed = False
            instances.append(self)
        def start(self): pass
        def write(self, data): self.events.put(('transcript', '도와줄 사람이 있어요'))
        def close(self): self.closed = True

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', FakeRecognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        headers = {'Authorization': f"Bearer {call['call_token']}"}
        assert client.post(f"/calls/{call['id']}/consent", json={'text': '동의'}, headers=headers).status_code == 200
        if already_urgent:
            assert client.post(f"/calls/{call['id']}/urgent", headers=headers).status_code == 200
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech", headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json() == {'type': 'ready'}
            if not already_urgent:
                assert client.post(f"/calls/{call['id']}/urgent", headers=headers).status_code == 200
            ws.send_bytes(b'\x00\x00' * 320)
            assert ws.receive_json() == {'type': 'transcript', 'text': '도와줄 사람이 있어요'}
        assert instances[0].closed


def test_ended_call_cannot_start_or_continue_speech() -> None:
    from app.azure_stt import _speech_active

    assert not _speech_active({'state': 'accepted', 'urgent': True, 'ended_at': 'now'})
    assert not _speech_active({'state': 'accepted', 'urgent': False, 'ended_at': 'now'})
    assert not _speech_active({'state': 'awaiting_consent', 'urgent': True})
    assert _speech_active({'state': 'accepted', 'urgent': True})
