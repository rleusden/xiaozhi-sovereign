from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator

from websockets.asyncio.client import ClientConnection, connect

from .config import Config


class OpenAIRealtime:
    def __init__(self, config: Config, identity: str) -> None:
        self.config = config
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()

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
            raise RuntimeError("OpenAI did not create a Realtime session")
        await self._send(self._session_update(None))
        while True:
            event = await asyncio.wait_for(self._recv_json(), timeout=15)
            if event.get("type") == "session.updated":
                return
            if event.get("type") == "error":
                raise RuntimeError("OpenAI rejected the Realtime session settings")

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

    async def set_mode(self, mode: str) -> None:
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

    async def append_pcm(self, pcm: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def commit_and_respond(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})

    async def send_text(self, text: str) -> None:
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
        if response:
            await self._send({"type": "response.cancel"})
        if clear_audio:
            await self._send({"type": "input_audio_buffer.clear"})

    async def events(self) -> AsyncIterator[dict]:
        if self.ws is None:
            raise RuntimeError("OpenAI connection not open")
        async for raw in self.ws:
            if isinstance(raw, str):
                yield json.loads(raw)

    async def _recv_json(self) -> dict:
        if self.ws is None:
            raise RuntimeError("OpenAI connection not open")
        raw = await self.ws.recv()
        if not isinstance(raw, str):
            raise RuntimeError("unexpected binary OpenAI event")
        return json.loads(raw)

    async def _send(self, payload: dict) -> None:
        if self.ws is None:
            raise RuntimeError("OpenAI connection not open")
        async with self._send_lock:
            await self.ws.send(json.dumps(payload, separators=(",", ":")))

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
