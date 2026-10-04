from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any, Protocol

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .config import Config
from .logging_safe import device_ref, event
from .openai_realtime import OpenAIRealtime
from .opus import OpusCodec, OpusError
from .resample import Resample16To24


class State(str, Enum):
    AWAIT_HELLO = "await_hello"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    CLOSED = "closed"


class Realtime(Protocol):
    async def open(self) -> None: ...
    async def set_mode(self, mode: str) -> None: ...
    async def append_pcm(self, pcm: bytes) -> None: ...
    async def commit_and_respond(self) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def cancel(self, *, response: bool, clear_audio: bool) -> None: ...
    def events(self): ...
    async def close(self) -> None: ...


class DeviceSession:
    JSON_LIMIT = 8192
    AUDIO_FRAME_BYTES = 1440 * 2

    def __init__(
        self,
        ws: ServerConnection,
        config: Config,
        *,
        realtime: Realtime | None = None,
        codec: OpusCodec | None = None,
    ) -> None:
        self.ws = ws
        self.config = config
        self.session_id = str(uuid.uuid4())
        self.state = State.AWAIT_HELLO
        self.started = time.monotonic()
        self.last_activity = self.started
        self.device_id = ws.request.headers.get("Device-Id", "")
        self.client_id = ws.request.headers.get("Client-Id", "")
        self.ref = device_ref(self.device_id, self.client_id)
        self.realtime = realtime or OpenAIRealtime(
            config, f"{self.device_id}\0{self.client_id}"
        )
        self.codec = codec or OpusCodec()
        self.resampler = Resample16To24()
        self.mode = "auto"
        self.input_samples = 0
        self.output_pcm = bytearray()
        self.output_queue: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue(64)
        self.turn = 0
        self.tts_started = False
        self.tts_stopped = True
        self.sentence_started = False
        self.output_text = ""
        self.responses: dict[str, int] = {}
        self.prelisten_packets = 0
        self._detect_task: asyncio.Task | None = None
        self._detect_submitted = False
        self._send_lock = asyncio.Lock()
        self._upstream_task: asyncio.Task | None = None
        self._output_task: asyncio.Task | None = None
        self.log = logging.getLogger("xiaozhi.session")

    async def run(self) -> None:
        try:
            self._authorize()
            raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
            hello = self._parse_json(raw)
            self._validate_hello(hello)
            await self._send_json(
                {
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "session_id": self.session_id,
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 24000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
            self.state = State.IDLE
            event(self.log, "device_connected", device=self.ref)
            await self.realtime.open()
            self._upstream_task = asyncio.create_task(self._read_upstream())
            self._output_task = asyncio.create_task(self._send_output())
            while True:
                timeout = min(
                    120,
                    self.config.max_session_seconds
                    - (time.monotonic() - self.started),
                )
                if timeout <= 0:
                    await self.ws.close(1000, "session lifetime reached")
                    break
                try:
                    message = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
                except TimeoutError:
                    await self.ws.close(1001, "idle timeout")
                    break
                self.last_activity = time.monotonic()
                if isinstance(message, bytes):
                    await self._on_audio(message)
                else:
                    await self._on_control(self._parse_json(message))
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except PermissionError as exc:
            await self.ws.close(1008, str(exc))
        except (ValueError, OpusError) as exc:
            event(self.log, "protocol_error", device=self.ref, reason=type(exc).__name__)
            await self.ws.close(1008, "protocol violation")
        except Exception as exc:
            event(self.log, "session_error", device=self.ref, reason=type(exc).__name__)
            await self.ws.close(1011, "gateway error")
        finally:
            self.state = State.CLOSED
            for task in (self._upstream_task, self._output_task, self._detect_task):
                if task is not None:
                    task.cancel()
            await self.realtime.close()
            self.codec.close()
            event(self.log, "device_disconnected", device=self.ref)

    def _authorize(self) -> None:
        headers = self.ws.request.headers
        if self.ws.request.path != "/xiaozhi/ws":
            raise PermissionError("wrong endpoint")
        if headers.get("Protocol-Version") != "1":
            raise PermissionError("unsupported protocol")
        presented = headers.get("Authorization", "")
        expected = f"Bearer {self.config.device_token}"
        if not hmac.compare_digest(presented, expected):
            raise PermissionError("unauthorized")
        if not self.device_id or not self.client_id:
            raise PermissionError("missing device identity")
        if self.config.allowed_device_id and not hmac.compare_digest(
            self.device_id.casefold(), self.config.allowed_device_id.casefold()
        ):
            raise PermissionError("device not allowed")

    def _parse_json(self, raw: str | bytes) -> dict[str, Any]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.JSON_LIMIT:
            raise ValueError("expected bounded JSON text")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("JSON message must be an object")
        return value

    def _validate_hello(self, message: dict[str, Any]) -> None:
        audio = message.get("audio_params")
        expected = {
            "format": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration": 60,
        }
        if (
            message.get("type") != "hello"
            or message.get("version") != 1
            or message.get("transport") != "websocket"
            or not isinstance(audio, dict)
            or any(audio.get(key) != value for key, value in expected.items())
        ):
            raise ValueError("incompatible hello")

    async def _on_control(self, message: dict[str, Any]) -> None:
        if message.get("session_id") != self.session_id:
            raise ValueError("wrong session id")
        kind = message.get("type")
        if kind == "abort":
            await self._abort()
            return
        if kind != "listen":
            raise ValueError("unsupported control message")
        action = message.get("state")
        if action == "start":
            mode = message.get("mode", "auto")
            if mode not in {"auto", "manual", "realtime"}:
                raise ValueError("unsupported listen mode")
            if self.state not in {State.IDLE, State.SPEAKING, State.PROCESSING}:
                raise ValueError("listen/start in invalid state")
            coalesced_detect = (
                self.state == State.PROCESSING
                and self._detect_task is not None
                and not self._detect_task.done()
                and not self._detect_submitted
            )
            if coalesced_detect:
                self._detect_task.cancel()
                self._detect_task = None
            elif self.state in {State.SPEAKING, State.PROCESSING}:
                await self._abort()
            self.turn += 1
            self._reset_turn()
            self.mode = mode
            self.prelisten_packets = 0
            await self.realtime.set_mode(mode)
            self.state = State.LISTENING
            event(self.log, "listen_start", device=self.ref, mode=mode)
        elif action == "stop":
            if self.state != State.LISTENING:
                raise ValueError("listen/stop in invalid state")
            self.state = State.PROCESSING
            tail = self.resampler.flush()
            if tail:
                await self.realtime.append_pcm(tail)
            if self.mode == "manual":
                if self.input_samples == 0:
                    raise ValueError("empty manual turn")
                await self.realtime.commit_and_respond()
            elif self.input_samples:
                # An explicit client stop wins over server VAD. Switching VAD
                # off and committing are ordered on the upstream WebSocket.
                await self.realtime.set_mode("manual")
                await self.realtime.commit_and_respond()
            event(self.log, "listen_stop", device=self.ref, mode=self.mode)
        elif action == "detect":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise ValueError("invalid detect text")
            if self.state in {State.SPEAKING, State.PROCESSING}:
                await self._abort()
            self.turn += 1
            self._reset_turn()
            self.state = State.PROCESSING
            await self._send_json({"type": "stt", "session_id": self.session_id, "text": text})
            self._detect_submitted = False
            self._detect_task = asyncio.create_task(
                self._submit_detect_after_grace(self.turn, text)
            )
            event(self.log, "listen_detect", device=self.ref)
        else:
            raise ValueError("unsupported listen state")

    async def _on_audio(self, packet: bytes) -> None:
        if self.state == State.IDLE:
            # Stock firmware may upload bounded wake-word pre-roll before
            # listen/detect. The text event already carries the wake word, so
            # discard this sensitive audio rather than retaining it.
            self.prelisten_packets += 1
            if self.prelisten_packets > 50 or not 0 < len(packet) <= OpusCodec.MAX_PACKET:
                raise ValueError("excessive pre-listen audio")
            return
        if self.state in {State.PROCESSING, State.SPEAKING} and self.mode == "auto":
            # Stock firmware may keep its uplink active briefly, or throughout
            # response generation. VAD already ended this turn, so validate and
            # discard the trailing packet instead of forwarding it upstream.
            if not 0 < len(packet) <= OpusCodec.MAX_PACKET:
                raise ValueError("invalid trailing audio packet")
            return
        if self.state != State.LISTENING and not (
            self.state == State.SPEAKING and self.mode == "realtime"
        ):
            raise ValueError("binary audio outside listening state")
        pcm16 = self.codec.decode_16k(packet)
        self.input_samples += len(pcm16) // 2
        if self.input_samples > self.config.max_turn_seconds * 16000:
            raise ValueError("turn audio limit exceeded")
        pcm24 = self.resampler.process(pcm16)
        if pcm24:
            await self.realtime.append_pcm(pcm24)

    async def _submit_detect_after_grace(self, turn: int, text: str) -> None:
        try:
            await asyncio.sleep(0.25)
            if turn != self.turn or self.state != State.PROCESSING:
                return
            self._detect_submitted = True
            await self.realtime.set_mode("manual")
            await self.realtime.send_text(text)
        except asyncio.CancelledError:
            pass

    async def _read_upstream(self) -> None:
        try:
            async for message in self.realtime.events():
                kind = message.get("type")
                if kind == "conversation.item.input_audio_transcription.completed":
                    text = message.get("transcript")
                    if isinstance(text, str) and text:
                        await self._send_json(
                            {"type": "stt", "session_id": self.session_id, "text": text}
                        )
                elif kind == "response.created":
                    response = message.get("response", {})
                    response_id = response.get("id") if isinstance(response, dict) else None
                    if not isinstance(response_id, str):
                        raise RuntimeError("OpenAI response has no id")
                    self.responses[response_id] = self.turn
                    self.state = State.SPEAKING
                    self.tts_started = True
                    self.tts_stopped = False
                    await self._send_json(
                        {"type": "tts", "state": "start", "session_id": self.session_id}
                    )
                elif kind == "response.output_audio_transcript.delta":
                    if self.responses.get(message.get("response_id")) != self.turn:
                        continue
                    delta = message.get("delta")
                    if isinstance(delta, str):
                        self.output_text += delta
                elif kind == "response.output_audio.delta":
                    if self.responses.get(message.get("response_id")) != self.turn:
                        continue
                    delta = message.get("delta")
                    if isinstance(delta, str):
                        self.output_pcm.extend(base64.b64decode(delta, validate=True))
                        await self._queue_complete_frames()
                elif kind == "response.done":
                    response = message.get("response", {})
                    response_id = response.get("id") if isinstance(response, dict) else None
                    response_turn = self.responses.pop(response_id, None)
                    if response_turn == self.turn:
                        await self._finish_response()
                elif kind == "input_audio_buffer.speech_stopped":
                    if self.mode in {"auto", "realtime"} and self.state == State.LISTENING:
                        self.state = State.PROCESSING
                elif kind == "input_audio_buffer.speech_started":
                    if self.mode == "realtime":
                        self.input_samples = 0
                elif kind == "error":
                    raise RuntimeError("OpenAI Realtime error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event(self.log, "upstream_error", device=self.ref, reason=type(exc).__name__)
            await self.ws.close(1011, "upstream error")

    async def _queue_complete_frames(self) -> None:
        while len(self.output_pcm) >= self.AUDIO_FRAME_BYTES:
            frame = bytes(self.output_pcm[: self.AUDIO_FRAME_BYTES])
            del self.output_pcm[: self.AUDIO_FRAME_BYTES]
            await self.output_queue.put((self.turn, self.codec.encode_24k(frame)))

    async def _finish_response(self) -> None:
        if self.output_pcm:
            self.output_pcm.extend(b"\0" * (self.AUDIO_FRAME_BYTES - len(self.output_pcm)))
            await self._queue_complete_frames()
        await self.output_queue.put((self.turn, None))

    async def _send_output(self) -> None:
        while True:
            turn, packet = await self.output_queue.get()
            try:
                if turn != self.turn:
                    continue
                if packet is None:
                    await self._stop_tts()
                    self.state = State.LISTENING if self.mode == "realtime" else State.IDLE
                    event(self.log, "response_complete", device=self.ref)
                    continue
                if not self.sentence_started:
                    await self._send_json(
                        {
                            "type": "tts",
                            "state": "sentence_start",
                            "session_id": self.session_id,
                            "text": self.output_text.strip() or "…",
                        }
                    )
                    self.sentence_started = True
                async with self._send_lock:
                    await self.ws.send(packet)
                await asyncio.sleep(0.06)
            finally:
                self.output_queue.task_done()

    async def _abort(self) -> None:
        old_turn = self.turn
        had_response = old_turn in self.responses.values()
        cancel_response = had_response or self.state in {State.PROCESSING, State.SPEAKING}
        had_audio = self.input_samples > 0 and self.state in {
            State.LISTENING,
            State.PROCESSING,
        }
        self.turn += 1
        if self._detect_task is not None:
            self._detect_task.cancel()
            self._detect_task = None
        try:
            await self.realtime.cancel(response=cancel_response, clear_audio=had_audio)
        except Exception:
            pass
        self.output_pcm.clear()
        while True:
            try:
                self.output_queue.get_nowait()
                self.output_queue.task_done()
            except asyncio.QueueEmpty:
                break
        # The wire contract requires one stop for every accepted abort.
        self.tts_stopped = False
        await self._stop_tts()
        self._reset_turn()
        self.state = State.IDLE
        event(self.log, "aborted", device=self.ref)

    async def _stop_tts(self) -> None:
        if not self.tts_stopped:
            await self._send_json(
                {"type": "tts", "state": "stop", "session_id": self.session_id}
            )
            self.tts_stopped = True
            self.tts_started = False

    def _reset_turn(self) -> None:
        self.resampler.reset()
        self.input_samples = 0
        self.output_pcm.clear()
        self.output_text = ""
        self.tts_started = False
        self.tts_stopped = True
        self.sentence_started = False
        self._detect_submitted = False

    async def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            await self.ws.send(encoded)
