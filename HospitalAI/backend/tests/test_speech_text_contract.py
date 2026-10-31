"""Barge-in echo suppression receives the exact text sent for synthesis."""

import base64
import json
import sqlite3
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from app.call_search import CallDatabase
from app.audio_store import AudioStore
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.orchestrator import ServiceBundle
from app.recommend_api import add_audio
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import TTSResult, WAIT_MESSAGE


class Synthesizer:
    def __init__(self) -> None:
        self.sent: dict[str, str] = {}

    async def deliver(self, text: str, kind: str) -> TTSResult:
        self.sent[kind] = text
        return TTSResult('synthesized', kind, text, False, b'audio', 'audio/mpeg')


class DistinctVoiceModel:
    async def low(self, text: str) -> str:
        return WAIT_MESSAGE

    async def reasoning(self, text: str, sources: list[dict[str, str]]) -> str:
        return json.dumps({'display_text': '화면에 보이는 내용', 'tts_text': '음성으로 말할 내용 [clinic:test]'})


def test_final_metadata_matches_synthesized_speech_not_display(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(CallDatabase, 'search', lambda self, patient_id, text: [{'id': 'clinic:test', 'text': '자료'}])
    db = Database(tmp_path / 'voice.sqlite')
    voice = Synthesizer()
    app = create_app(Settings(environment='development', service_token='staff'), db, ServiceBundle(DistinctVoiceModel(), voice))
    with TestClient(app, headers={'Authorization': 'Bearer staff'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        client.post(f'/calls/{call_id}/consent', json={'text': '동의'})
        client.post(f'/calls/{call_id}/messages', json={'request_id': 'one', 'text': '기침이 납니다'})
        retrieved = client.get(f'/calls/{call_id}/messages/one')
        assert retrieved.headers['Cache-Control'] == 'no-store'
        turn = retrieved.json()
        assert turn['speech_text'] == voice.sent['final'] == '음성으로 말할 내용'
        assert turn['speech_text'] != turn['final_text']
        client.post(f'/calls/{call_id}/messages/one/interrupt', json={})
        interrupted = client.get(f'/calls/{call_id}/messages/one').json()
        assert interrupted.get('speech_text') is None


def test_prompt_audio_headers_encode_exact_spoken_text(tmp_path: Path) -> None:
    db = Database(tmp_path / 'prompt.sqlite')
    voice = Synthesizer()
    app = create_app(Settings(environment='development', service_token='staff'), db, ServiceBundle(DistinctVoiceModel(), voice))
    with TestClient(app, headers={'Authorization': 'Bearer staff'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        audio = client.get(f'/calls/{call_id}/prompts/consent/audio')
        assert audio.status_code == 200
        assert base64.b64decode(audio.headers['X-Spoken-Text-Base64']).decode('utf-8') == voice.sent['prompt']


def test_recommendation_speech_metadata_matches_audio_request() -> None:
    query = RecommendationQuery(latitude=35.0, longitude=126.0)
    result = recommend(query, [HospitalCandidate(id='one', name='가까운병원', phone='062-123-4567',
                                                  latitude=35.0, longitude=126.0, source='test')])
    voice = Synthesizer()
    response = anyio.run(add_audio, result, voice, 3.0)
    assert response.speech_text == voice.sent['recommendation']


def test_legacy_audio_migration_never_invents_the_spoken_text(tmp_path: Path) -> None:
    db = Database(tmp_path / 'legacy.sqlite')
    db.initialize()
    session = db.create_session('+821000000001')
    turn, _ = db.create_turn(str(session['id']), 'old', '기침')
    with sqlite3.connect(db.path) as connection:
        connection.execute('CREATE TABLE generated_audio (turn_id TEXT NOT NULL, kind TEXT NOT NULL, content BLOB NOT NULL, mime_type TEXT NOT NULL, PRIMARY KEY(turn_id,kind))')
        connection.execute('INSERT INTO generated_audio VALUES (?,?,?,?)', (turn['id'], 'final', b'old', 'audio/mpeg'))
    store = AudioStore(db)
    store.initialize()
    old = store.get(str(turn['id']), 'final')
    assert old is not None and old.data == b'old' and old.spoken_text is None


def test_recommendation_without_audio_has_no_spoken_text() -> None:
    query = RecommendationQuery(latitude=35.0, longitude=126.0)
    result = recommend(query, [HospitalCandidate(id='one', name='가까운병원', latitude=35.0, longitude=126.0, source='test')])

    class FailedSpeech:
        async def deliver(self, text: str, kind: str) -> TTSResult:
            return TTSResult('simulated', kind, text, False, b'not-synthesized', 'audio/mpeg')

    response = anyio.run(add_audio, result, FailedSpeech(), 3.0)
    assert response.audio_status == 'unavailable'
    assert response.speech_text is None


def test_prompt_rejects_audio_without_successful_synthesis(tmp_path: Path) -> None:
    class FailedSpeech:
        async def deliver(self, text: str, kind: str) -> TTSResult:
            return TTSResult('simulated', kind, text, False, b'not-synthesized', 'audio/mpeg')

    db = Database(tmp_path / 'prompt-failed.sqlite')
    app = create_app(Settings(environment='development', service_token='staff'), db, ServiceBundle(DistinctVoiceModel(), FailedSpeech()))
    with TestClient(app, headers={'Authorization': 'Bearer staff'}) as client:
        call_id = client.post('/calls', json={'phone': '+821000000001'}).json()['id']
        response = client.get(f'/calls/{call_id}/prompts/consent/audio')
        assert response.status_code == 503
        assert 'X-Spoken-Text-Base64' not in response.headers
