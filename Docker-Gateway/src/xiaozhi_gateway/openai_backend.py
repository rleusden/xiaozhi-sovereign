from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect

from .backend import (
    BackendError,
    BackendEvent,
    ResponseAudio,
    ResponseDone,
    ResponseStarted,
    ResponseText,
    UserSpeechEnded,
    UserSpeechStarted,
    UserTranscript,
)
from .config import Config
from .resample import Resample16To24


class OpenAIBackend:
    """ConversationBackend on top of one OpenAI Realtime WebSocket session.

    OpenAI Realtime wants and returns PCM16 at 24 kHz, so the 16 kHz device
    audio is resampled here. Output already matches the backend contract.
    """

    def __init__(self, config: Config, identity: str) -> None:
        self.config = config
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._resampler = Resample16To24()
        self._turn = 0
        self._mode = "auto"
        self._responses: dict[str, int] = {}

    async def open(self) -> None:
        url = f"wss://api.openai.com/v1/realtime?model={self.config.openai_model}"
        self.ws = await connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "OpenAI-Safety-Identifier": self.identity,
            },
            compression=None,
            max_size=2 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=15,
        )
        first = await asyncio.wait_for(self._recv_json(), timeout=15)
        if first.get("type") != "session.created":
            raise BackendError("OpenAI did not create a Realtime session")
        await self._send(self._session_update(None))
        while True:
            event = await asyncio.wait_for(self._recv_json(), timeout=15)
            if event.get("type") == "session.updated":
                return
            if event.get("type") == "error":
                raise BackendError("OpenAI rejected the Realtime session settings")

    # -- ConversationBackend -------------------------------------------------

    async def start_turn(self, turn: int, mode: str) -> None:
        self._turn = turn
        self._mode = mode
        self._resampler.reset()
        await self._set_turn_detection(mode)

    async def append_audio(self, pcm16k: bytes) -> None:
        pcm24 = self._resampler.process(pcm16k)
        if pcm24:
            await self._append(pcm24)

    async def end_turn(self) -> None:
        tail = self._resampler.flush()
        if tail:
            await self._append(tail)
        if self._mode != "manual":
            # An explicit client stop wins over server VAD. Switching VAD off
            # and committing are ordered on the upstream WebSocket.
            await self._set_turn_detection("manual")
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})

    async def send_text(self, turn: int, text: str) -> None:
        self._turn = turn
        self._resampler.reset()
        await self._set_turn_detection("manual")
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def cancel(self, *, response: bool, clear_audio: bool) -> None:
        self._resampler.reset()
        if response or self._turn in self._responses.values():
            await self._send({"type": "response.cancel"})
        if clear_audio:
            await self._send({"type": "input_audio_buffer.clear"})

    async def events(self) -> AsyncIterator[BackendEvent]:
        if self.ws is None:
            raise BackendError("OpenAI connection not open")
        async for raw in self.ws:
            if not isinstance(raw, str):
                continue
            event = self._translate(json.loads(raw))
            if event is not None:
                yield event

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    # -- OpenAI Realtime details ---------------------------------------------

    def _translate(self, message: dict) -> BackendEvent | None:
        kind = message.get("type")
        if kind == "conversation.item.input_audio_transcription.completed":
            text = message.get("transcript")
            if isinstance(text, str) and text:
                return UserTranscript(text)
        elif kind == "response.created":
            response_id = self._response_id(message)
            if response_id is None:
                raise BackendError("OpenAI response has no id")
            self._responses[response_id] = self._turn
            return ResponseStarted(self._turn)
        elif kind == "response.output_audio_transcript.delta":
            turn = self._responses.get(message.get("response_id"))
            delta = message.get("delta")
            if turn is not None and isinstance(delta, str):
                return ResponseText(turn, delta)
        elif kind == "response.output_audio.delta":
            turn = self._responses.get(message.get("response_id"))
            delta = message.get("delta")
            if turn is not None and isinstance(delta, str):
                return ResponseAudio(turn, base64.b64decode(delta, validate=True))
        elif kind == "response.done":
            turn = self._responses.pop(self._response_id(message), None)
            if turn is not None:
                return ResponseDone(turn)
        elif kind == "input_audio_buffer.speech_started":
            return UserSpeechStarted()
        elif kind == "input_audio_buffer.speech_stopped":
            return UserSpeechEnded()
        elif kind == "error":
            raise BackendError("OpenAI Realtime error")
        return None

    @staticmethod
    def _response_id(message: dict) -> str | None:
        response = message.get("response")
        response_id = response.get("id") if isinstance(response, dict) else None
        return response_id if isinstance(response_id, str) else None

    def _session_update(self, turn_detection: dict | None) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.config.openai_model,
                "output_modalities": ["audio"],
                "instructions": self.config.instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": self.config.transcription_model},
                        "turn_detection": turn_detection,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.config.voice,
                    },
                },
            },
        }

    async def _set_turn_detection(self, mode: str) -> None:
        detection = None
        if mode in {"auto", "realtime"}:
            detection = {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 700,
                "create_response": True,
                "interrupt_response": True,
            }
        await self._send(self._session_update(detection))

    async def _append(self, pcm24: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm24).decode("ascii"),
            }
        )

    async def _recv_json(self) -> dict:
        if self.ws is None:
            raise BackendError("OpenAI connection not open")
        raw = await self.ws.recv()
        if not isinstance(raw, str):
            raise BackendError("unexpected binary OpenAI event")
        return json.loads(raw)

    async def _send(self, payload: dict) -> None:
        if self.ws is None:
            raise BackendError("OpenAI connection not open")
        async with self._send_lock:
            await self.ws.send(json.dumps(payload, separators=(",", ":")))
