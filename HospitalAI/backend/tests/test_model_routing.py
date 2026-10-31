from app.providers import build_services
from app.services import SplitLLM


def test_call_decisions_use_luna_and_waiting_uses_the_separate_small_model(monkeypatch):
    for name, value in {
        'HOSPITAL_LOW_PROVIDER':'gemini', 'GEMINI_API_KEY':'waiting-test', 'GEMINI_MODEL':'waiting-only',
        'HOSPITAL_HIGH_PROVIDER':'openai-compatible', 'HOSPITAL_HIGH_BASE_URL':'https://api.openai.com/v1',
        'HOSPITAL_HIGH_API_KEY':'decision-test', 'HOSPITAL_HIGH_MODEL':'gpt-6-luna',
        'HOSPITAL_ALLOW_MEDICAL_DATA':'true', 'HOSPITAL_TTS_PROVIDER':'mock',
    }.items():
        monkeypatch.setenv(name, value)
    model = build_services().llm
    assert isinstance(model, SplitLLM)
    assert model.decision_provider is model.high_provider
    assert model.high_provider.config.low_model == 'gpt-6-luna'
    assert model.low_provider.config.low_model == 'waiting-only'
