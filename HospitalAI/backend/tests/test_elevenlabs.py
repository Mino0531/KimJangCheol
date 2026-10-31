import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import anyio
import pytest

from app.elevenlabs import ElevenLabsConfig, ElevenLabsTTS
from app.services import ServiceError


def test_tts_converts_text_with_configured_voice_and_returns_audio() -> None:
    received: list[tuple[str, str, bytes]] = []
    audio = b'ID3\x04\x00test-mp3'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append((self.path, self.headers['xi-api-key'], self.rfile.read(int(self.headers['content-length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)

        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        config = ElevenLabsConfig(api_key='local-key', voice_id='voice123', base_url=f'http://127.0.0.1:{server.server_port}')
        result = anyio.run(ElevenLabsTTS(config).deliver, '안녕하세요', 'final')
        worker.join(timeout=5)

    assert result.status == 'synthesized'
    assert result.audio == audio
    assert result.mime_type == 'audio/mpeg'
    assert result.interruptible is False
    assert received[0][0] == '/v1/text-to-speech/voice123?output_format=mp3_44100_128'
    assert received[0][1] == 'local-key'
    assert json.loads(received[0][2]) == {'text': '안녕하세요', 'model_id': 'eleven_flash_v2_5', 'language_code': 'ko'}


def test_tts_refuses_non_audio_provider_response() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        config = ElevenLabsConfig(api_key='local-key', voice_id='voice123', base_url=f'http://127.0.0.1:{server.server_port}')
        with pytest.raises(ServiceError):
            anyio.run(ElevenLabsTTS(config).deliver, '안녕하세요', 'low')
        worker.join(timeout=5)


def test_configuration_rejects_insecure_remote_and_hides_key() -> None:
    with pytest.raises(ServiceError):
        ElevenLabsConfig(api_key='private-secret', voice_id='voice123', base_url='http://example.com')
    config = ElevenLabsConfig(api_key='private-secret', voice_id='voice123')
    assert 'private-secret' not in repr(config)


def test_concurrent_call_speeds_are_sent_per_request() -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers['content-length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.end_headers()
            self.wfile.write(b'ID3audio')

        def log_message(self, format: str, *args: str) -> None:
            return

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        config = ElevenLabsConfig(api_key='local-key', voice_id='voice123', base_url=f'http://127.0.0.1:{server.server_port}')
        tts = ElevenLabsTTS(config)

        async def make_requests() -> None:
            async with anyio.create_task_group() as tasks:
                async def speak(text: str, speed: float) -> None:
                    await tts.deliver(text, 'final', speed=speed)
                tasks.start_soon(speak, 'slow call', 0.85)
                tasks.start_soon(speak, 'normal call', 1.0)

        try:
            anyio.run(make_requests)
        finally:
            server.shutdown()
            worker.join(timeout=5)

    assert {body['text']: body['voice_settings']['speed'] for body in received} == {'slow call': 0.85, 'normal call': 1.0}


@pytest.mark.parametrize('speed', [0.69, 1.21, float('nan'), float('inf')])
def test_invalid_speech_rate_fails_before_network(speed: float) -> None:
    tts = ElevenLabsTTS(ElevenLabsConfig(api_key='local-key', voice_id='voice123'))

    async def speak() -> None:
        await tts.deliver('안녕하세요', 'final', speed=speed)

    with pytest.raises(ServiceError):
        anyio.run(speak)
