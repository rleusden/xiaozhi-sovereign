from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
import uuid
import wave
from collections.abc import AsyncIterator
from contextlib import aclosing

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
from .http_client import HttpClient, HttpError
from .logging_safe import event
from .vad import EnergyVad

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_MARKUP = re.compile(r"[*_#`]+")
_LANGUAGE_NAMES = {
    "nl": "dutch",
    "en": "english",
    "fr": "french",
    "de": "german",
    "es": "spanish",
    "it": "italian",
    "pt": "portuguese",
}
_SPOKEN_STYLE = (
    " Je antwoord wordt voorgelezen: schrijf gewone zinnen zonder opmaak, "
    "lijstjes, emoji of afkortingen."
)


class MistralBackend:
    """ConversationBackend as a pipeline: Voxtral STT -> chat model -> Voxtral TTS.

    Mistral has no single realtime speech-to-speech session, so this backend
    decides the end of a turn itself (``EnergyVad``), keeps the conversation
    history in memory for the lifetime of the device session, and synthesises
    the answer sentence by sentence while the model is still writing.
    """

    HISTORY_MESSAGES = 12
    MAX_TOKENS = 300
    # Mistral answers HTTP 429 when a rate limit is hit, for example the
    # requests-per-second limit of the free plan. One turn makes several
    # requests in quick succession, so wait and try again.
    RATE_LIMIT_DELAYS = (1.1, 2.0, 3.0)
    # Shortest text for the first speech request (about two seconds of audio).
    MIN_FIRST_CHARS = 40

    def __init__(self, config: Config, identity: str) -> None:
        self.config = config
        self.http = HttpClient(
            config.mistral_base_url,
            {"Authorization": f"Bearer {config.mistral_api_key}"},
        )
        self.vad = EnergyVad(
            threshold=config.vad_threshold, silence_ms=config.vad_silence_ms
        )
        self.voice_id = config.mistral_voice_id
        self.gain = float(getattr(config, "mistral_tts_gain", 1.0))
        self.max_sentences = int(getattr(config, "mistral_max_sentences", 3))
        self._tts_peak = 0.0
        self.log = logging.getLogger("xiaozhi.mistral")
        self._events: asyncio.Queue[BackendEvent | BaseException] = asyncio.Queue()
        self._history: list[dict[str, str]] = []
        self._turn = 0
        self._mode = "auto"
        self._capturing = False
        self._manual_audio = bytearray()
        self._pipeline: asyncio.Task | None = None

    # -- ConversationBackend -------------------------------------------------

    async def open(self) -> None:
        if not self.voice_id:
            self.voice_id = await self._default_voice()
        else:
            await self.http.warm()

    async def start_turn(self, turn: int, mode: str) -> None:
        self._log_unheard()
        self._turn = turn
        self._mode = mode
        self._manual_audio.clear()
        self.vad.reset()
        self._capturing = True

    async def append_audio(self, pcm16k: bytes) -> None:
        if not self._capturing:
            return
        if self._mode == "manual":
            self._manual_audio.extend(pcm16k)
            return
        change = self.vad.feed(pcm16k)
        if change == "start":
            self._emit(UserSpeechStarted())
        elif change == "end":
            peak = self.vad.peak
            threshold = int(self.vad.threshold)
            speech = self.vad.take_speech()
            self._capturing = False
            event(
                self.log,
                "speech_end",
                ms=len(speech) // 32,
                peak=peak,
                threshold=threshold,
            )
            self._emit(UserSpeechEnded())
            self._start_pipeline(audio=speech)

    async def end_turn(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        if self._mode == "manual":
            speech = bytes(self._manual_audio)
            self._manual_audio.clear()
        else:
            speech = self.vad.take_speech()
        self._start_pipeline(audio=speech)

    async def send_text(self, turn: int, text: str) -> None:
        self._turn = turn
        self._capturing = False
        self._start_pipeline(text=text)

    async def cancel(self, *, response: bool, clear_audio: bool) -> None:
        self._log_unheard()
        self._capturing = False
        self._manual_audio.clear()
        self.vad.reset()
        await self._stop_pipeline()

    async def events(self) -> AsyncIterator[BackendEvent]:
        while True:
            item = await self._events.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def close(self) -> None:
        self._log_unheard()
        self._capturing = False
        await self._stop_pipeline()
        self.http.close()
        self._history.clear()

    def _log_unheard(self) -> None:
        """Report a listening period in which no speech was detected.

        The loudest level against the threshold shows whether VAD_THRESHOLD is
        set too high for the microphone.
        """
        if self._capturing and self._mode != "manual" and not self.vad.in_speech:
            event(
                self.log,
                "no_speech",
                loudest=self.vad.loudest,
                threshold=int(self.vad.threshold),
            )

    # -- pipeline ------------------------------------------------------------

    def _emit(self, item: BackendEvent | BaseException) -> None:
        self._events.put_nowait(item)

    def _start_pipeline(self, *, audio: bytes | None = None, text: str | None = None) -> None:
        if self._pipeline is not None and not self._pipeline.done():
            self._pipeline.cancel()
        self._pipeline = asyncio.create_task(self._run(self._turn, audio, text))

    async def _stop_pipeline(self) -> None:
        task, self._pipeline = self._pipeline, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self, turn: int, audio: bytes | None, text: str | None) -> None:
        started = time.monotonic()
        timing: dict[str, int] = {}

        def mark(name: str) -> None:
            timing.setdefault(name, int((time.monotonic() - started) * 1000))

        self._tts_peak = 0.0
        try:
            if text is None:
                text = await self._transcribe(audio or b"")
                mark("stt_ms")
                if text:
                    self._emit(UserTranscript(text))
            self._emit(ResponseStarted(turn))
            answer = ""
            if text:
                answer = await self._respond(turn, text, mark)
            self._emit(ResponseDone(turn))
            mark("done_ms")
            if text and answer:
                self._history += [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": answer},
                ]
                del self._history[: -self.HISTORY_MESSAGES]
            # tts_peak: loudest sample Mistral delivered, in percent of full
            # scale and before MISTRAL_TTS_GAIN is applied.
            event(self.log, "turn_timing", tts_peak=int(self._tts_peak * 100), **timing)
            if self._mode == "realtime":
                self.vad.reset()
                self._capturing = True
        except asyncio.CancelledError:
            raise
        except BackendError as exc:
            self._emit(exc)
        except Exception as exc:
            self._emit(BackendError(f"Mistral pipeline failed: {type(exc).__name__}"))

    async def _respond(self, turn: int, text: str, mark) -> str:
        """Stream the model's answer and speak it sentence by sentence."""
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speak(turn, sentences, mark))
        kept: list[str] = []
        limit = self.max_sentences
        try:
            pending = ""
            # A model does not reliably obey "answer briefly", so the limit is
            # enforced here: after ``limit`` sentences the stream is closed and
            # the rest of the answer is neither generated nor spoken.
            async with aclosing(self._chat(text)) as deltas:
                async for delta in deltas:
                    mark("llm_first_ms")
                    pending += delta
                    parts = _SENTENCE_END.split(pending)
                    pending = parts[-1]
                    for sentence in parts[:-1]:
                        if not limit or len(kept) < limit:
                            self._keep(turn, sentence, kept, sentences)
                    if limit and len(kept) >= limit:
                        pending = ""
                        break
            self._keep(turn, pending, kept, sentences)
            # The text is complete now; its audio follows.
            self._emit(ResponseText(turn, "", final=True))
            mark("text_done_ms")
            sentences.put_nowait(None)
            await speaker
        finally:
            speaker.cancel()
        return _clean(" ".join(kept))

    def _keep(self, turn: int, sentence: str, kept: list[str], sentences: asyncio.Queue) -> None:
        """Accept one sentence of the answer: show it and queue it for speech."""
        sentence = _clean(sentence)
        if sentence:
            kept.append(sentence)
            self._emit(ResponseText(turn, sentence + " "))
            sentences.put_nowait(sentence)

    async def _speak(self, turn: int, sentences: asyncio.Queue, mark) -> None:
        """Synthesise the answer in a few requests and play them in order.

        The first request is sent as soon as there is enough text, for a quick
        start. Each later request is sent as soon as its text is written, while
        earlier audio is still arriving, so that its audio is ready before the
        previous part has finished playing.
        """
        streams: asyncio.Queue[asyncio.Queue | None] = asyncio.Queue()
        producers: list[asyncio.Task] = []

        async def produce(text: str, out: asyncio.Queue) -> None:
            try:
                async for pcm in self._synthesise(text):
                    out.put_nowait(pcm)
                out.put_nowait(None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                out.put_nowait(exc)

        async def plan() -> None:
            finished = False
            first = True
            while not finished:
                chunk = [await sentences.get()]
                while chunk[-1] is not None:
                    # Take everything that is already written. A very short
                    # opening would be over before the next part can arrive,
                    # so the first request waits for more text.
                    if sentences.empty() and not (
                        first and len(" ".join(chunk)) < self.MIN_FIRST_CHARS
                    ):
                        break
                    chunk.append(await sentences.get())
                if chunk[-1] is None:
                    finished = True
                    chunk.pop()
                spoken = _clean(" ".join(chunk))
                if spoken:
                    first = False
                    out: asyncio.Queue = asyncio.Queue()
                    producers.append(asyncio.create_task(produce(spoken, out)))
                    streams.put_nowait(out)
            streams.put_nowait(None)

        planner = asyncio.create_task(plan())
        try:
            while (stream := await streams.get()) is not None:
                while (item := await stream.get()) is not None:
                    if isinstance(item, Exception):
                        raise item
                    mark("tts_first_ms")
                    self._emit(ResponseAudio(turn, item))
            await planner
        finally:
            planner.cancel()
            for task in producers:
                task.cancel()

    # -- Mistral API ---------------------------------------------------------

    async def _transcribe(self, pcm16k: bytes) -> str:
        if len(pcm16k) < 3200:  # under 100 ms: nothing was said
            return ""
        wav = io.BytesIO()
        with wave.open(wav, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(pcm16k)
        fields = {"model": self.config.mistral_stt_model}
        if self.config.mistral_language:
            fields["language"] = self.config.mistral_language
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            'name="file"; filename="turn.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        body += wav.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        data = await self._call(
            "transcription",
            lambda: self.http.request(
                "POST",
                "/v1/audio/transcriptions",
                body=bytes(body),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            ),
        )
        text = json.loads(data).get("text")
        return text.strip() if isinstance(text, str) else ""

    async def _chat(self, text: str) -> AsyncIterator[str]:
        body = {
            "model": self.config.mistral_model,
            "stream": True,
            "max_tokens": self.MAX_TOKENS,
            "messages": [
                {
                    "role": "system",
                    "content": self.config.instructions + _SPOKEN_STYLE + self._now(),
                },
                *self._history,
                {"role": "user", "content": text},
            ],
        }
        async for payload in self._sse("chat", "/v1/chat/completions", body):
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            content = (choices[0].get("delta") or {}).get("content")
            if isinstance(content, str):
                if content:
                    yield content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        yield part["text"]

    def _now(self) -> str:
        """The local date and time, so the model can answer questions about today.

        Off unless MISTRAL_TELL_DATE is set: a small model may announce the
        date in every answer instead of using it only when asked.
        """
        if not getattr(self.config, "mistral_tell_date", False):
            return ""
        offset = timedelta(minutes=getattr(self.config, "timezone_offset_minutes", 0))
        now = datetime.now(timezone(offset))
        return now.strftime(
            " Background information, never mention it unless the user asks about"
            " the date or time: it is now %A %Y-%m-%d %H:%M local time."
        )

    async def _synthesise(self, text: str) -> AsyncIterator[bytes]:
        body = {
            "model": self.config.mistral_tts_model,
            "input": text,
            "voice_id": self.voice_id,
            "response_format": "pcm",
            "stream": True,
        }
        rest = b""  # bytes of a float32 sample split across two events
        async for payload in self._sse("speech", "/v1/audio/speech", body):
            if payload.get("type") == "speech.audio.delta":
                audio = payload.get("audio_data")
                if isinstance(audio, str):
                    pcm, rest = self._float32_to_pcm16(rest + base64.b64decode(audio))
                    if pcm:
                        yield pcm

    async def _default_voice(self) -> str:
        """Pick a preset voice for the configured language."""
        data = await self._call(
            "voice list",
            lambda: self.http.request("GET", "/v1/audio/voices?type=preset&limit=100"),
        )
        voices = json.loads(data).get("items")
        if not isinstance(voices, list) or not voices:
            raise BackendError("Mistral returned no preset voices; set MISTRAL_VOICE_ID")
        wanted = (self.config.mistral_language or "").casefold()
        names = (wanted, _LANGUAGE_NAMES.get(wanted, wanted))
        for voice in voices:
            languages = [str(item).casefold() for item in voice.get("languages") or []]
            if wanted and any(item.startswith(names) for item in languages):
                return str(voice["id"])
        raise BackendError(
            "no preset Mistral voice for the configured language; set MISTRAL_VOICE_ID"
        )

    async def _sse(self, stage: str, path: str, body: dict) -> AsyncIterator[dict]:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        attempt = 0
        while True:
            lines = self.http.stream_lines(
                path,
                body=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            )
            try:
                # A refused request fails before the first line, so a retry
                # never repeats output that was already passed on.
                async for line in lines:
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    payload = json.loads(data)
                    if isinstance(payload, dict):
                        yield payload
                return
            except HttpError as exc:
                if not await self._wait_for_rate_limit(stage, exc, attempt):
                    raise BackendError(f"Mistral {stage} failed: HTTP {exc.status}") from None
                attempt += 1
            except (OSError, ValueError) as exc:
                raise BackendError(f"Mistral {stage} failed: {type(exc).__name__}") from None
            finally:
                await lines.aclose()

    async def _call(self, stage: str, send) -> bytes:
        attempt = 0
        while True:
            try:
                return await send()
            except HttpError as exc:
                if not await self._wait_for_rate_limit(stage, exc, attempt):
                    raise BackendError(f"Mistral {stage} failed: HTTP {exc.status}") from None
                attempt += 1
            except (OSError, ValueError) as exc:
                raise BackendError(f"Mistral {stage} failed: {type(exc).__name__}") from None

    async def _wait_for_rate_limit(self, stage: str, exc: HttpError, attempt: int) -> bool:
        """Sleep before a retry of a rate-limited request; False when giving up."""
        if exc.status != 429 or attempt >= len(self.RATE_LIMIT_DELAYS):
            return False
        delay = self.RATE_LIMIT_DELAYS[attempt]
        if exc.retry_after is not None:
            delay = min(max(exc.retry_after, 0.2), 5.0)
        event(self.log, "rate_limited", stage=stage, wait_ms=int(delay * 1000))
        await asyncio.sleep(delay)
        return True

    def _float32_to_pcm16(self, data: bytes) -> tuple[bytes, bytes]:
        """Convert float32 little-endian samples to PCM16.

        Returns the PCM and the trailing bytes of an incomplete sample.
        """
        usable = len(data) - len(data) % 4
        rest = data[usable:]
        samples = array.array("f")
        samples.frombytes(data[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        if samples:
            peak = max(max(samples), -min(samples))
            if peak == peak:  # not NaN
                self._tts_peak = max(self._tts_peak, min(peak, 1.0))
        gain = self.gain
        out = array.array(
            "h",
            (
                int(32767 * (1.0 if v > 1.0 else -1.0 if v < -1.0 else v)) if v == v else 0
                for v in (s * gain for s in samples)
            ),
        )
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes(), rest


def _clean(text: str) -> str:
    return " ".join(_MARKUP.sub("", text).split())
