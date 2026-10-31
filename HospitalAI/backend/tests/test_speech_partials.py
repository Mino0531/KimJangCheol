"""Provisional STT results never replace the final utterance."""

from pathlib import Path

from tests.test_azure_stt import make_client, new_call


def test_partial_and_final_results_share_segment(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    class Recognizer:
        def __init__(self, key, region, events):
            self.events = events

        def start(self):
            pass

        def write(self, data):
            self.events.emit_partial('아', 1)
            self.events.emit_partial('아파요', 1)
            self.events.emit_final('아파요.', 1)

        def close(self):
            pass

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', Recognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech",
                                      headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json()['type'] == 'ready'
            ws.send_bytes(b'\x00\x00' * 320)
            assert ws.receive_json() == {'type': 'partial', 'text': '아파요', 'segment': 1}
            assert ws.receive_json() == {'type': 'transcript', 'text': '아파요.', 'segment': 1}


def test_many_interim_updates_do_not_overflow_or_drop_final() -> None:
    from app.azure_stt import SpeechEvents

    events = SpeechEvents()
    for index in range(1000):
        events.emit_partial(f'말씀 {index}', 3)
    events.emit_final('말씀 끝.', 3)

    assert not events.overflow.is_set()
    assert events.get_nowait() == ('partial', '', 3)
    assert events.take_partial(3) == '말씀 999'
    assert events.get_nowait() == ('transcript', '말씀 끝.', 3)


def test_failed_recognition_closes_the_interim_segment(tmp_path: Path, monkeypatch) -> None:
    from app import azure_stt

    class Recognizer:
        def __init__(self, key, region, events):
            self.events = events

        def start(self):
            pass

        def write(self, data):
            self.events.emit_partial('아', 1)
            self.events.emit_no_match(1)

        def close(self):
            pass

    monkeypatch.setattr(azure_stt, 'AzureRecognizer', Recognizer)
    with make_client(tmp_path) as client:
        call = new_call(client)
        with client.websocket_connect(f"ws://127.0.0.1:8000/calls/{call['id']}/speech",
                                      headers={'Origin': 'http://127.0.0.1:8000'}) as ws:
            ws.send_json({'type': 'start', 'token': call['call_token'], 'stt_notice_acknowledged': True})
            assert ws.receive_json()['type'] == 'ready'
            ws.send_bytes(b'\x00\x00' * 320)
            assert ws.receive_json()['type'] == 'partial'
            assert ws.receive_json() == {'type': 'no_match', 'segment': 1, 'reason': 'unrecognized'}
