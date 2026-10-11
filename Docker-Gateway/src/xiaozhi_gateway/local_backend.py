"""Fully local XiaoZhi backend.

STT runs through the local faster-whisper service, language-model inference
through local Ollama/Qwen, and TTS through the local Supertonic service.

No external AI provider is required by this backend.
"""

from __future__ import annotations

import json
import io
import wave
from array import array
import logging
from collections.abc import AsyncIterator

from .backend import BackendError
from .config import Config
from .http_client import HttpClient, HttpError
from .pipeline_backend import PipelineBackend

_SPOKEN_STYLE = (
    " Antwoord altijd in het Nederlands, tenzij de gebruiker expliciet om "
    "een andere taal of een vertaling vraagt. "
    "Je antwoord wordt voorgelezen: schrijf gewone, natuurlijke Nederlandse "
    "zinnen zonder opmaak, lijstjes, emoji of onnodige afkortingen."
)


class LocalBackend(PipelineBackend):
    """Fully local speech pipeline: faster-whisper -> Ollama/Qwen -> Supertonic."""

    def __init__(self, config: Config, identity: str) -> None:
        super().__init__(config, identity)
        self.ollama = HttpClient(
            config.ollama_base_url,
            {},
            timeout=float(config.ollama_timeout_seconds),
        )
        self.local_stt = HttpClient(
            config.local_stt_base_url,
            {},
            timeout=float(config.local_stt_timeout_seconds),
        )

        self.local_tts = HttpClient(
            config.local_tts_base_url,
            {},
            timeout=float(config.local_tts_timeout_seconds),
        )

        self.log = logging.getLogger("xiaozhi.local")

    async def open(self) -> None:
        await super().open()
        # Warm the TCP connection when the compute node is already awake. The
        # helper intentionally ignores connect failures; the first request will
        # surface a clean BackendError if Ollama is unavailable.
        await self.ollama.warm()
        await self.local_stt.warm()
        await self.local_tts.warm()

    async def close(self) -> None:
        self.ollama.close()
        self.local_stt.close()
        self.local_tts.close()

    async def _transcribe(self, pcm16k: bytes) -> str:
        try:
            raw = await self.local_stt.request(
                "POST",
                "/transcribe",
                body=pcm16k,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Accept": "application/json",
                },
            )
            payload = json.loads(raw.decode("utf-8"))
        except HttpError as exc:
            raise BackendError(f"Local STT failed: HTTP {exc.status}") from None
        except OSError as exc:
            raise BackendError(
                f"Local STT failed: {type(exc).__name__}"
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(
                f"Local STT failed: invalid response ({type(exc).__name__})"
            ) from None

        if not isinstance(payload, dict):
            raise BackendError("Local STT failed: invalid response")

        text = payload.get("text")
        if not isinstance(text, str):
            raise BackendError("Local STT failed: invalid transcript")

        return text.strip()

        return text.strip()


    @staticmethod
    def _wav_to_pcm24(data: bytes) -> bytes:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())

        if channels != 1 or width != 2:
            raise BackendError(
                f"Local TTS returned unsupported WAV: {channels}ch/{width * 8}bit"
            )

        samples = array("h")
        samples.frombytes(frames)

        if rate == 24000:
            return samples.tobytes()

        if len(samples) < 2:
            return samples.tobytes()

        out_len = int(len(samples) * 24000 / rate)
        out = array("h")

        for i in range(out_len):
            pos = i * rate / 24000
            left = int(pos)
            frac = pos - left
            right = min(left + 1, len(samples) - 1)
            value = samples[left] + (samples[right] - samples[left]) * frac
            out.append(max(-32768, min(32767, int(value))))

        return out.tobytes()

    async def _synthesise(self, text: str) -> AsyncIterator[bytes]:
        body = json.dumps(
            {"text": text},
            separators=(",", ":"),
        ).encode("utf-8")

        try:
            wav = await self.local_tts.request(
                "POST",
                "/speak",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "audio/wav",
                },
            )
        except HttpError as exc:
            raise BackendError(
                f"Local TTS failed: HTTP {exc.status}"
            ) from None
        except OSError as exc:
            raise BackendError(
                f"Local TTS failed: {type(exc).__name__}"
            ) from None

        pcm24 = self._wav_to_pcm24(wav)

        if pcm24:
            samples = array("h")
            samples.frombytes(pcm24)
            if samples:
                peak = max(abs(sample) for sample in samples) / 32768.0
                self._tts_peak = max(self._tts_peak, min(peak, 1.0))

        chunk_bytes = 24000 * 2 // 10
        for offset in range(0, len(pcm24), chunk_bytes):
            yield pcm24[offset:offset + chunk_bytes]


    async def _chat(self, text: str) -> AsyncIterator[str]:
        """Stream newline-delimited Ollama ``/api/chat`` response fragments."""
        body = {
            "model": self.config.ollama_model,
            "stream": True,
            "messages": [
                {
                    "role": "system",
                    "content": self.config.instructions + _SPOKEN_STYLE + self._now(),
                },
                *self._history,
                {"role": "user", "content": text},
            ],
            "keep_alive": "30m",
            "options": {
                "num_predict": self.MAX_TOKENS,
            },
        }
        encoded = json.dumps(body, separators=(",", ":")).encode()
        lines = self.ollama.stream_lines(
            "/api/chat",
            body=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
            },
        )
        try:
            async for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BackendError("Ollama chat failed: invalid JSON stream") from exc
                if not isinstance(payload, dict):
                    continue
                error = payload.get("error")
                if isinstance(error, str) and error:
                    raise BackendError("Ollama chat failed: provider error")
                message = payload.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        yield content
                if payload.get("done") is True:
                    return
        except HttpError as exc:
            raise BackendError(f"Ollama chat failed: HTTP {exc.status}") from None
        except OSError as exc:
            raise BackendError(f"Ollama chat failed: {type(exc).__name__}") from None
        finally:
            await lines.aclose()
