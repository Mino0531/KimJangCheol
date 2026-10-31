from pathlib import Path

import anyio

from app.services import INSUFFICIENT_MESSAGE, MockLLM, MockTTS, emergency_detected


def test_emergency_detected_when_breathing_stops() -> None:
    assert emergency_detected('숨을 쉴 수 없어요')


def test_routine_question_when_no_emergency() -> None:
    assert not emergency_detected('무릎이 아파요')


def test_missing_evidence_returns_insufficient() -> None:
    answer = anyio.run(MockLLM().reasoning, '무릎이 아파요', [])
    assert answer == INSUFFICIENT_MESSAGE


def test_mock_tts_does_not_claim_audio_was_played() -> None:
    result = anyio.run(MockTTS().deliver, '대기 안내', 'low')
    assert result.status == 'simulated'
    assert result.interruptible is False


def test_configuration_rejects_unapproved_medical_data() -> None:
    import pytest
    from app.services import OpenAIConfig, ServiceError
    from app.services import OpenAICompatibleLLM
    config = OpenAIConfig(base_url='https://example.com/v1', api_key='x', reasoning_model='b')
    with pytest.raises(ServiceError):
        anyio.run(OpenAICompatibleLLM(config).reasoning, 'question', [{'id': 'clinic:1', 'text': 'source'}])


def test_configuration_rejects_remote_plain_http() -> None:
    import pytest
    from app.services import OpenAIConfig, ServiceError
    with pytest.raises(ServiceError):
        OpenAIConfig(base_url='http://example.com/v1', api_key='x', low_model='a', reasoning_model='b', allow_medical_data=True)


def test_split_provider_routes_independently() -> None:
    from app.services import SplitLLM

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name
        async def low(self, text: str) -> str:
            return self.name
        async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
            return self.name

    provider = SplitLLM(low_provider=Provider('fast'), high_provider=Provider('deep'))
    assert anyio.run(provider.low, 'question') == 'fast'
    assert anyio.run(provider.reasoning, 'question', []) == 'deep'



def test_low_provider_sends_no_patient_input_over_http() -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from app.services import OpenAICompatibleLLM, OpenAIConfig, WAIT_MESSAGE

    bodies: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            bodies.append(self.rfile.read(int(self.headers['content-length'])))
            payload = json.dumps({'choices': [{'message': {'content': 'A'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        config = OpenAIConfig(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='test', low_model='low')
        answer = anyio.run(OpenAICompatibleLLM(config).low, 'patient-secret-182')
        worker.join(timeout=5)
    assert answer == WAIT_MESSAGE
    assert len(bodies) == 1
    assert b'patient-secret-182' not in bodies[0]


def test_malformed_provider_response_reports_service_error() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import pytest
    from app.services import OpenAICompatibleLLM, OpenAIConfig, ServiceError

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers['content-length']))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
        def log_message(self, format: str, *args: str) -> None:
            return

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.handle_request)
        worker.start()
        config = OpenAIConfig(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='test', low_model='low')
        with pytest.raises(ServiceError):
            anyio.run(OpenAICompatibleLLM(config).low, 'test')
        worker.join(timeout=5)


def test_reasoning_discards_unknown_citation() -> None:
    from app.services import OpenAICompatibleLLM, OpenAIConfig

    class InvalidCitationProvider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return 'unsupported [clinic:999]'

    config = OpenAIConfig(base_url='https://example.com/v1', api_key='x', reasoning_model='high', allow_medical_data=True)
    answer = anyio.run(InvalidCitationProvider(config).reasoning, 'question', [{'id': 'clinic:1', 'text': 'source'}])
    assert answer == INSUFFICIENT_MESSAGE


def test_low_provider_selects_only_approved_message() -> None:
    from app.services import OpenAICompatibleLLM, OpenAIConfig, WAIT_MESSAGES

    class ChoiceProvider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return 'B'

    config = OpenAIConfig(base_url='https://example.com/v1', api_key='x', low_model='low')
    answer = anyio.run(ChoiceProvider(config).low, 'patient secret')
    assert answer == WAIT_MESSAGES[1]


def test_low_provider_rejects_unapproved_output() -> None:
    from app.services import OpenAICompatibleLLM, OpenAIConfig, WAIT_MESSAGE

    class UnsafeProvider(OpenAICompatibleLLM):
        async def _complete(self, model: str, system: str, user: str) -> str:
            return 'an unapproved clinical claim'

    config = OpenAIConfig(base_url='https://example.com/v1', api_key='x', low_model='low')
    assert anyio.run(UnsafeProvider(config).low, 'patient secret') == WAIT_MESSAGE


def test_orchestrator_delivers_selected_approved_message(tmp_path: Path) -> None:
    from app.db import Database
    from app.orchestrator import ServiceBundle, process_turn
    from app.services import MockLLM, MockTTS, TTSResult, WAIT_MESSAGES

    db = Database(tmp_path / 'selection.sqlite')
    db.initialize(seed=True)
    session = db.create_session('+821000000001', 'sample-patient-1')
    turn, _ = db.create_turn(str(session['id']), 'select-1', '기침이 있어요')

    async def scenario() -> None:
        delivered = anyio.Event()

        class SelectedLLM(MockLLM):
            async def low(self, text: str) -> str:
                return WAIT_MESSAGES[1]
            async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
                await delivered.wait()
                return 'final'

        class ObservedTTS(MockTTS):
            async def deliver(self, text: str, kind: str) -> TTSResult:
                if kind == 'low':
                    delivered.set()
                return await super().deliver(text, kind)

        with anyio.fail_after(5):
            await process_turn(db, ServiceBundle(SelectedLLM(), ObservedTTS()), session, turn)

    anyio.run(scenario)
    stored = db.get_turn(str(session['id']), 'select-1')
    assert stored is not None and stored['initial_text'] == WAIT_MESSAGES[1]

