"""Explicit, billable provider smoke test using only non-patient sample text."""

from time import monotonic

import anyio

from app.config import load_environment
from app.providers import build_services


async def check() -> None:
    load_environment()
    services = build_services()
    started = monotonic()
    await services.llm.low("provider connectivity test")
    print(f"Configured low provider responded in {(monotonic() - started) * 1000:.0f} ms (check whether mock is configured)")
    started = monotonic()
    audio = await services.tts.deliver("안녕하세요. 음성 연결을 확인합니다.", "low")
    print(f"TTS status: {audio.status}; {(monotonic() - started) * 1000:.0f} ms")


if __name__ == "__main__":
    anyio.run(check)
