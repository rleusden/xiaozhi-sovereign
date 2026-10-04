from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from .backend import (
    BackendError,
    ConversationBackend,
    ResponseAudio,
    ResponseDone,
    ResponseStarted,
    ResponseText,
    UserSpeechEnded,
    UserSpeechStarted,
    UserTranscript,
    create_backend,
)
from .config import Config
from .logging_safe import device_ref, event
from .opus import OpusCodec, OpusError


class State(str, Enum):
    AWAIT_HELLO = "await_hello"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    CLOSED = "closed"


def _reason(exc: Exception) -> dict[str, str]:
    fields = {"reason": type(exc).__name__}
    if isinstance(exc, BackendError):
        # By contract a BackendError message carries no provider payload.
        fields["detail"] = str(exc)
    return fields


class DeviceSession:
    JSON_LIMIT = 8192
    AUDIO_FRAME_BYTES = 1440 * 2
    # Encoded answer audio waiting to be sent at playback speed. It holds a
    # whole answer (90 s) so that reading backend events, such as the answer
    # text, is not held up until the audio has almost finished playing.
    OUTPUT_QUEUE_FRAMES = 1500

    def __init__(
        self,
        ws: ServerConnection,
        config: Config,
        *,
        backend: ConversationBackend | None = None,
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
        self.backend = backend or create_backend(
            config, f"{self.device_id}\0{self.client_id}"
        )
        self.codec = codec or OpusCodec()
        self.mode = "auto"
        self.input_samples = 0
        self.output_pcm = bytearray()
        self.output_queue: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue(
            self.OUTPUT_QUEUE_FRAMES
        )
        self.turn = 0
        self.tts_started = False
        self.tts_stopped = True
        self.sentence_started = False
        self.output_text = ""
        self.output_text_final = False
        self.user_text_sent = False
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
            await self.backend.open()
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
            event(self.log, "session_error", device=self.ref, **_reason(exc))
            await self.ws.close(1011, "gateway error")
        finally:
            self.state = State.CLOSED
            for task in (self._upstream_task, self._output_task, self._detect_task):
                if task is not None:
                    task.cancel()
            await self.backend.close()
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
            await self.backend.start_turn(self.turn, mode)
            self.state = State.LISTENING
            event(self.log, "listen_start", device=self.ref, mode=mode)
        elif action == "stop":
            if self.state != State.LISTENING:
                raise ValueError("listen/stop in invalid state")
            self.state = State.PROCESSING
            if self.mode == "manual" and self.input_samples == 0:
                raise ValueError("empty manual turn")
            if self.input_samples:
                # An explicit client stop wins over backend turn detection.
                await self.backend.end_turn()
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
            self.user_text_sent = True
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
        await self.backend.append_audio(pcm16)

    async def _submit_detect_after_grace(self, turn: int, text: str) -> None:
        try:
            await asyncio.sleep(0.25)
            if turn != self.turn or self.state != State.PROCESSING:
                return
            self._detect_submitted = True
            await self.backend.send_text(turn, text)
        except asyncio.CancelledError:
            pass

    async def _read_upstream(self) -> None:
        try:
            async for item in self.backend.events():
                if isinstance(item, UserTranscript):
                    await self._send_json(
                        {"type": "stt", "session_id": self.session_id, "text": item.text}
                    )
                    self.user_text_sent = True
                    await self._send_response_text()
                elif isinstance(item, UserSpeechEnded):
                    if self.mode in {"auto", "realtime"} and self.state == State.LISTENING:
                        self.state = State.PROCESSING
                elif isinstance(item, UserSpeechStarted):
                    self.user_text_sent = False
                    if self.mode == "realtime":
                        self.input_samples = 0
                elif item.turn != self.turn:
                    # Output of a turn that was aborted or superseded.
                    continue
                elif isinstance(item, ResponseStarted):
                    self.state = State.SPEAKING
                    self.tts_started = True
                    self.tts_stopped = False
                    self.output_text = ""
                    self.output_text_final = False
                    self.sentence_started = False
                    await self._send_json(
                        {"type": "tts", "state": "start", "session_id": self.session_id}
                    )
                elif isinstance(item, ResponseText):
                    self.output_text += item.text
                    if item.final:
                        self.output_text_final = True
                        await self._send_response_text()
                elif isinstance(item, ResponseAudio):
                    self.output_pcm.extend(item.pcm)
                    await self._queue_complete_frames()
                elif isinstance(item, ResponseDone):
                    self.output_text_final = True
                    await self._send_response_text()
                    await self._finish_response()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event(self.log, "upstream_error", device=self.ref, **_reason(exc))
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
                    # The user's transcript never arrived; show the answer anyway.
                    await self._send_response_text(force=True)
                    await self._stop_tts()
                    self.state = State.LISTENING if self.mode == "realtime" else State.IDLE
                    event(self.log, "response_complete", device=self.ref)
                    continue
                async with self._send_lock:
                    await self.ws.send(packet)
                await asyncio.sleep(0.06)
            finally:
                self.output_queue.task_done()

    async def _send_response_text(self, *, force: bool = False) -> None:
        """Send the answer text once: complete, and after the user's text.

        The device appends one bubble per message and cannot update it, so the
        text is held until the response is complete. It is also held until the
        user's transcript has been sent, because a backend may deliver that
        transcript after the response has started.
        """
        if self.sentence_started or not self.tts_started:
            return
        if not force and not (self.output_text_final and self.user_text_sent):
            return
        text = self.output_text.strip()
        if not text:
            return
        self.sentence_started = True
        await self._send_json(
            {
                "type": "tts",
                "state": "sentence_start",
                "session_id": self.session_id,
                "text": text,
            }
        )

    async def _abort(self) -> None:
        cancel_response = self.state in {State.PROCESSING, State.SPEAKING}
        had_audio = self.input_samples > 0 and self.state in {
            State.LISTENING,
            State.PROCESSING,
        }
        self.turn += 1
        if self._detect_task is not None:
            self._detect_task.cancel()
            self._detect_task = None
        try:
            await self.backend.cancel(response=cancel_response, clear_audio=had_audio)
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
        self.input_samples = 0
        self.output_pcm.clear()
        self.output_text = ""
        self.output_text_final = False
        self.user_text_sent = False
        self.tts_started = False
        self.tts_stopped = True
        self.sentence_started = False
        self._detect_submitted = False

    async def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with self._send_lock:
            await self.ws.send(encoded)
