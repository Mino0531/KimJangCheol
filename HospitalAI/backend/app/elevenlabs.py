"""ElevenLabs HTTP speech synthesis; playback is a caller responsibility."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx2

from app.services import ServiceError, TTSResult


@dataclass(frozen=True, slots=True)
class ElevenLabsConfig:
    api_key: str = field(repr=False)
    voice_id: str
    model_id: str = "eleven_flash_v2_5"
    base_url: str = "https://api.elevenlabs.io"
    output_format: str = "mp3_44100_128"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        address = urlparse(self.base_url)
        local_http = address.scheme == "http" and address.hostname in {"localhost", "127.0.0.1", "::1"}
        if not ((address.scheme == "https" and address.hostname) or local_http):
            raise ServiceError("ElevenLabs URL must use HTTPS or loopback HTTP")
        if address.username or address.password or address.query or address.fragment:
            raise ServiceError("ElevenLabs URL cannot contain credentials, query or fragment")
        if not self.api_key or not re.fullmatch(r"[A-Za-z0-9_-]+", self.voice_id):
            raise ServiceError("A valid voice ID and API key are required")
        if not self.model_id or not self.output_format.startswith("mp3_") or self.timeout_seconds <= 0:
            raise ServiceError("A model, MP3 output format and positive timeout are required")


class ElevenLabsTTS:
    """Convert a complete utterance to MP3 without implying it was played."""

    def __init__(self, config: ElevenLabsConfig) -> None:
        self.config = config

    async def deliver(self, text: str, kind: str, *, speed: float | None = None) -> TTSResult:
        """Synthesize using an optional rate scoped to this request, never shared state."""
        if speed is not None and not 0.7 <= speed <= 1.2:
            raise ServiceError("ElevenLabs speech speed must be between 0.7 and 1.2")
        limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30.0)
        transport = httpx2.AsyncHTTPTransport(
            http2=True, retries=3, limits=limits,
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        )
        timeout = httpx2.Timeout(connect=5, read=self.config.timeout_seconds, write=10, pool=10)
        body: dict[str, str | dict[str, float]] = {"text": text, "model_id": self.config.model_id}
        if speed is not None:
            body["voice_settings"] = {"speed": speed}
        if self.config.model_id != "eleven_multilingual_v2":
            body["language_code"] = "ko"
        async with httpx2.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self.config.base_url.rstrip('/')}/v1/text-to-speech/{self.config.voice_id}",
                params={"output_format": self.config.output_format},
                headers={"xi-api-key": self.config.api_key, "Accept": "audio/mpeg"},
                json=body,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/mpeg":
                    raise ServiceError("ElevenLabs returned an unexpected content type")
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > 10 * 1024 * 1024:
                        raise ServiceError("ElevenLabs audio exceeded the maximum size")
        if not audio:
            raise ServiceError("ElevenLabs returned empty audio")
        return TTSResult(status="synthesized", kind=kind, text=text, interruptible=False,
                         audio=bytes(audio), mime_type="audio/mpeg")
