"""Build independent model providers from explicitly supplied settings."""

from os import getenv

from app.elevenlabs import ElevenLabsConfig, ElevenLabsTTS
from app.orchestrator import LanguageModel, ServiceBundle, SpeechOutput
from app.services import MockLLM, MockTTS, OpenAICompatibleLLM, OpenAIConfig, ServiceError, SplitLLM


def build_services() -> ServiceBundle:
    low_mode = getenv("HOSPITAL_LOW_PROVIDER", "auto")
    high_mode = getenv("HOSPITAL_HIGH_PROVIDER", "mock")
    if low_mode == "auto":
        low_mode = "gemini" if getenv("GEMINI_API_KEY", "") else "mock"
    low: LanguageModel = MockLLM()
    high: LanguageModel = MockLLM()
    if low_mode == "gemini":
        key = getenv("GEMINI_API_KEY", "")
        if not key:
            raise ServiceError("GEMINI_API_KEY is required for Gemini")
        low = OpenAICompatibleLLM(OpenAIConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=key,
            low_model=getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            allow_medical_data=getenv("HOSPITAL_ALLOW_MEDICAL_DATA", "false").lower() == "true",
        ))
    elif low_mode != "mock":
        raise ServiceError("Unsupported low-latency provider")
    if high_mode in ("gemini", "openai-compatible"):
        if getenv("HOSPITAL_ALLOW_MEDICAL_DATA", "false").lower() != "true":
            raise ServiceError("External medical data transfer must be explicitly authorized")
        reasoning_effort = None
        if high_mode == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            key = getenv("GEMINI_API_KEY", "")
            model = getenv("GEMINI_HIGH_MODEL", "")
            reasoning_effort = getenv("GEMINI_HIGH_REASONING_EFFORT", "high")
            if reasoning_effort not in ("low", "medium", "high"):
                raise ServiceError("Gemini reasoning effort must be low, medium, or high")
        else:
            url = getenv("HOSPITAL_HIGH_BASE_URL", "")
            key = getenv("HOSPITAL_HIGH_API_KEY", "")
            model = getenv("HOSPITAL_HIGH_MODEL", "")
        if not url or not key or not model:
            raise ServiceError("High-reasoning provider URL, key and model are required")
        high = OpenAICompatibleLLM(OpenAIConfig(
            base_url=url, api_key=key, low_model=model, reasoning_model=model, allow_medical_data=True,
            reasoning_effort=reasoning_effort,
            timeout_seconds=120.0 if high_mode == "gemini" else 30.0,
        ))
    elif high_mode != "mock":
        raise ServiceError("Unsupported high-reasoning provider")
    tts_mode = getenv("HOSPITAL_TTS_PROVIDER", "auto")
    if tts_mode == "auto":
        tts_mode = "elevenlabs" if (getenv("ELEVENLABS_API_KEY", "") or getenv("ELEVENLABS_VOICE_ID", "")) else "mock"
    tts: SpeechOutput = MockTTS()
    tts_timeout = 3.0
    if tts_mode == "elevenlabs":
        if getenv("HOSPITAL_ALLOW_TTS_MEDICAL_DATA", "false").lower() != "true":
            raise ServiceError("ElevenLabs medical text transfer must be explicitly authorized")
        key = getenv("ELEVENLABS_API_KEY", "")
        voice_id = getenv("ELEVENLABS_VOICE_ID", "")
        if not key or not voice_id:
            raise ServiceError("ElevenLabs API key and voice ID are required")
        tts = ElevenLabsTTS(ElevenLabsConfig(
            api_key=key, voice_id=voice_id,
            model_id=getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"),
        ))
        tts_timeout = 30.0
    elif tts_mode != "mock":
        raise ServiceError("Unsupported TTS provider")
    return ServiceBundle(SplitLLM(low_provider=low, high_provider=high, interpretation_provider=high), tts,
                         reasoning_timeout=130.0 if high_mode == "gemini" else 45.0,
                         tts_timeout=tts_timeout)
