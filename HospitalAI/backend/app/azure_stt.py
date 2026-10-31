"""Call-scoped Korean speech recognition without retaining the input audio."""

from __future__ import annotations

import json
from collections.abc import Callable
from queue import Empty, Full, Queue
from threading import Event as ThreadEvent, Lock
from typing import Final, Literal

import anyio
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError

from app.call_capabilities import CallCapabilities
from app.call_store import CallStore
from app.config import Settings
from app.db import Row

Event = tuple[Literal["transcript", "error"], str] | tuple[Literal["transcript", "partial", "no_match"], str, int]
NoMatchKind = Literal["silence", "unrecognized"]
MAX_FRAME: Final = 64000
# 16 kHz, 16-bit mono is 32 kB/s. Allow bursts for browser scheduling and
# reconnect buffering while preventing a client from sending unbounded audio.
AUDIO_BYTES_PER_SECOND: Final = 40000
AUDIO_BURST_BYTES: Final = 128000


def _speech_active(call: Row | None) -> bool:
    if call is None or call['state'] == 'declined' or call.get('ended_at'):
        return False
    # Before consent only consent collection is permitted, never urgent follow-up.
    return not bool(call['urgent']) or call['state'] == 'accepted'


class SpeechEvents(Queue[Event]):
    """Bounded callback bridge that reports overload instead of losing speech."""
    def __init__(self) -> None:
        super().__init__(maxsize=32)
        self.overflow = ThreadEvent()
        self._partial_lock = Lock()
        self._partials: dict[int, str] = {}

    def emit(self, event: Event) -> None:
        try:
            self.put_nowait(event)
        except Full:
            self.overflow.set()

    def emit_partial(self, text: str, segment: int) -> None:
        """Coalesce interim hypotheses without displacing final results."""
        with self._partial_lock:
            scheduled = segment in self._partials
            self._partials[segment] = text
            if not scheduled:
                self.emit(("partial", "", segment))

    def take_partial(self, segment: int) -> str:
        with self._partial_lock:
            return self._partials.pop(segment, "")

    def emit_final(self, text: str, segment: int) -> None:
        self.emit(("transcript", text, segment))

    def emit_no_match(self, segment: int, reason: NoMatchKind = "unrecognized") -> None:
        self.emit(("no_match", reason, segment))


class StartSpeech(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["start"]
    token: str
    stt_notice_acknowledged: Literal[True]


class AzureRecognizer:
    """Adapter for the Azure SDK's background-thread callbacks."""

    def __init__(self, key: str, region: str, events: SpeechEvents) -> None:
        import azure.cognitiveservices.speech as speechsdk

        config = speechsdk.SpeechConfig(subscription=key, region=region)
        config.speech_recognition_language = "ko-KR"
        config.set_property(speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, "1200")
        # Silence prompts are scheduled by the call flow; continuous recognition
        # must not emit a misleading empty recognition result every 15 seconds.
        config.set_property(speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "0")
        audio_format = speechsdk.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16, channels=1)
        self.stream = speechsdk.audio.PushAudioInputStream(stream_format=audio_format)
        audio = speechsdk.audio.AudioConfig(stream=self.stream)
        self.recognizer = speechsdk.SpeechRecognizer(speech_config=config, audio_config=audio)
        self._segment = 1
        self._segment_lock = Lock()

        def recognizing(event) -> None:
            text = event.result.text.strip()[:2000]
            if text:
                with self._segment_lock:
                    events.emit_partial(text, self._segment)

        def recognized(event) -> None:
            with self._segment_lock:
                if event.result.reason == speechsdk.ResultReason.RecognizedSpeech and event.result.text.strip():
                    events.emit_final(event.result.text.strip()[:2000], self._segment)
                else:
                    details = getattr(event.result, "no_match_details", None)
                    reason: NoMatchKind = ("silence" if details is not None and
                                           details.reason == speechsdk.NoMatchReason.InitialSilenceTimeout
                                           else "unrecognized")
                    events.emit_no_match(self._segment, reason)
                self._segment += 1

        def cancelled(event) -> None:
            if event.reason == speechsdk.CancellationReason.Error:
                events.emit(("error", "speech_provider_failed"))

        self.recognizer.recognizing.connect(recognizing)
        self.recognizer.recognized.connect(recognized)
        self.recognizer.canceled.connect(cancelled)

    def start(self) -> None:
        self.recognizer.start_continuous_recognition_async().get()

    def write(self, data: bytes) -> None:
        self.stream.write(data)

    def close(self) -> None:
        self.stream.close()
        self.recognizer.stop_continuous_recognition_async().get()


def speech_router(settings: Settings, capabilities: CallCapabilities) -> APIRouter:
    router = APIRouter()
    active_calls: set[str] = set()
    calls = CallStore(capabilities.database)

    @router.websocket("/calls/{call_id}/speech")
    async def speech(websocket: WebSocket, call_id: str) -> None:
        # Browser callers must be same-origin; the credential is never in a URL.
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        if websocket.headers.get("origin") != f"{scheme}://{websocket.headers.get('host')}":
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            with anyio.fail_after(10):
                first = await websocket.receive()
            if first["type"] == "websocket.disconnect":
                return
            payload = first.get("text")
            if payload is None or len(payload) > 1024:
                await websocket.send_json({"type": "error", "code": "invalid_start"})
                await websocket.close(code=1008)
                return
            start = StartSpeech.model_validate_json(payload)
            capabilities.authorize_call(call_id, "Bearer " + start.token)
        except (ValidationError, HTTPException, TimeoutError, json.JSONDecodeError):
            await websocket.send_json({"type": "error", "code": "invalid_start"})
            await websocket.close(code=1008)
            return
        if call_id in active_calls:
            await websocket.send_json({"type": "error", "code": "speech_already_active"})
            await websocket.close(code=1008)
            return
        active_calls.add(call_id)
        try:
            call = await anyio.to_thread.run_sync(calls.get, call_id)
            if not _speech_active(call):
                await websocket.send_json({"type": "error", "code": "call_inactive"})
                await websocket.close(code=1008)
                return
            await recognition_session(websocket, settings, lambda: calls.get(call_id))
        finally:
            active_calls.discard(call_id)

    return router


async def recognition_session(websocket: WebSocket, settings: Settings,
                              get_call: Callable[[], dict | None] | None = None) -> None:
    """Own and always release the provider session after a successful creation."""
    if not settings.azure_speech_key or not settings.azure_speech_region:
        await websocket.send_json({"type": "error", "code": "speech_unavailable"})
        await websocket.close(code=1011)
        return

    events = SpeechEvents()
    try:
        recognizer = await anyio.to_thread.run_sync(AzureRecognizer, settings.azure_speech_key,
                                                     settings.azure_speech_region, events)
    except (ImportError, RuntimeError, OSError):
        await websocket.send_json({"type": "error", "code": "speech_unavailable"})
        await websocket.close(code=1011)
        return
    try:
        try:
            await anyio.to_thread.run_sync(recognizer.start)
        except (RuntimeError, OSError):
            await websocket.send_json({"type": "error", "code": "speech_unavailable"})
            await websocket.close(code=1011)
            return
        async def active() -> bool:
            if get_call is None:
                return True
            call = await anyio.to_thread.run_sync(get_call)
            return _speech_active(call)

        if not await active():
            await websocket.send_json({"type": "error", "code": "call_inactive"})
            await websocket.close(code=4003)
            return
        await websocket.send_json({"type": "ready"})
        socket_lock = anyio.Lock()
        ended = False

        async def stop_inactive() -> None:
            nonlocal ended
            async with socket_lock:
                if not ended:
                    ended = True
                    await websocket.send_json({"type": "error", "code": "call_inactive"})
                    await websocket.close(code=4003)
            group.cancel_scope.cancel()

        async def watch_call() -> None:
            while True:
                await anyio.sleep(0.2)
                if not await active():
                    await stop_inactive()
                    return

        async def send_results() -> None:
            while True:
                if events.overflow.is_set():
                    await websocket.send_json({"type": "error", "code": "speech_overflow"})
                    await websocket.close(code=1011)
                    group.cancel_scope.cancel()
                    return
                try:
                    event = await anyio.to_thread.run_sync(lambda: events.get(timeout=0.2), abandon_on_cancel=True)
                except Empty:
                    continue
                kind, value, *segment = event
                if not await active():
                    await stop_inactive()
                    return
                if kind == "error":
                    await websocket.send_json({"type": "error", "code": value})
                    await websocket.close(code=1011)
                    group.cancel_scope.cancel()
                    return
                if kind == "partial":
                    value = events.take_partial(segment[0])
                    if not value:
                        continue
                async with socket_lock:
                    if not ended:
                        message: dict[str, str | int] = {"type": kind}
                        if kind != "no_match":
                            message["text"] = value
                        else:
                            message["reason"] = value if value in {"silence", "unrecognized"} else "unrecognized"
                        if segment:
                            message["segment"] = segment[0]
                        await websocket.send_json(message)

        budget = float(AUDIO_BURST_BYTES)
        previous_frame_at = anyio.current_time()
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(send_results)
                group.start_soon(watch_call)
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    audio = message.get("bytes")
                    if not audio or len(audio) > MAX_FRAME or len(audio) % 2:
                        await websocket.close(code=1003)
                        break
                    now = anyio.current_time()
                    budget = min(float(AUDIO_BURST_BYTES), budget + (now - previous_frame_at) * AUDIO_BYTES_PER_SECOND)
                    previous_frame_at = now
                    if len(audio) > budget:
                        await websocket.close(code=1009)
                        break
                    budget -= len(audio)
                    if not await active():
                        await stop_inactive()
                        break
                    await anyio.to_thread.run_sync(recognizer.write, audio)
                group.cancel_scope.cancel()
        except WebSocketDisconnect:
            pass
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(recognizer.close)
