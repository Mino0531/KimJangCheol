"""Recommendation voice uses only the leading public hospital."""

import anyio

from app.recommend_api import add_audio
from app.recommendation import HospitalCandidate, RecommendationQuery, recommend
from app.services import TTSResult


def test_voice_is_short_and_mentions_only_top_hospital() -> None:
    query = RecommendationQuery(latitude=35.0, longitude=126.0)
    candidates = [
        HospitalCandidate(id="near", name="가까운병원", phone="062-123-4567", latitude=35.0, longitude=126.0, source="test"),
        HospitalCandidate(id="far", name="다른병원", phone="02-999-9999", latitude=35.1, longitude=126.0, source="test"),
    ]
    result = recommend(query, candidates)

    class Recorder:
        spoken = ""

        async def deliver(self, text: str, kind: str) -> TTSResult:
            self.spoken = text
            return TTSResult("synthesized", kind, text, False, b"test audio", "audio/mpeg")

    recorder = Recorder()
    response = anyio.run(add_audio, result, recorder, 3.0)

    assert response.audio_status == "synthesized"
    assert "가까운병원" in recorder.spoken
    assert "다른병원" not in recorder.spoken
    assert "062-123-4567" not in recorder.spoken
    assert "진료과" in recorder.spoken and "확인하지 못" in recorder.spoken
    assert "가까운 의료기관" in recorder.spoken
    assert "02-999-9999" not in recorder.spoken
    assert len(recorder.spoken) <= 100
    assert len(response.hospitals) == 2


def test_unverified_specialty_is_disclosed_in_recommendation_audio() -> None:
    query = RecommendationQuery(latitude=35.0, longitude=126.0, specialty="정형외과")
    result = recommend(query, [HospitalCandidate(id="one", name="가까운병원", latitude=35.0,
                                                  longitude=126.0, source="test")])

    class Recorder:
        spoken = ""

        async def deliver(self, text: str, kind: str) -> TTSResult:
            self.spoken = text
            return TTSResult("synthesized", kind, text, False, b"test audio", "audio/mpeg")

    recorder = Recorder()
    response = anyio.run(lambda: add_audio(result, recorder, 3.0, specialty_requested=True))

    assert response.audio_status == "synthesized"
    assert "진료과" in recorder.spoken and "확인하지 못" in recorder.spoken
