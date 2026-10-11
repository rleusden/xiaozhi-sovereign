from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timedelta, timezone

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
from .logging_safe import event
from .vad import EnergyVad


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_MARKUP = re.compile(r"[*_#`]+")


def _clean(text: str) -> str:
    return " ".join(_MARKUP.sub("", text).split())


class PipelineBackend:
    """Provider-neutral speech conversation pipeline.

    Owns VAD, turn lifecycle, event delivery, conversation history,
    response sentence planning and audio delivery.

    Subclasses provide STT, LLM and TTS implementations.
    """

    HISTORY_MESSAGES = 12
    MAX_TOKENS = 300
    MIN_FIRST_CHARS = 40

    def __init__(self, config: Config, identity: str) -> None:
        self.config = config
        self.identity = identity
        self.vad = EnergyVad(
            threshold=config.vad_threshold,
            silence_ms=config.vad_silence_ms,
        )
        self.max_sentences = int(
            getattr(config, "mistral_max_sentences", 3)
        )
        self._tts_peak = 0.0
        self.log = logging.getLogger("xiaozhi.pipeline")
        self._events: asyncio.Queue[BackendEvent | BaseException] = asyncio.Queue()
        self._history: list[dict[str, str]] = []
        self._turn = 0
        self._mode = "auto"
        self._capturing = False
        self._manual_audio = bytearray()
        self._pipeline: asyncio.Task | None = None

    async def open(self) -> None:
        pass

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
        self._history.clear()

    def _log_unheard(self) -> None:
        if (
            self._capturing
            and self._mode != "manual"
            and not self.vad.in_speech
        ):
            event(
                self.log,
                "no_speech",
                loudest=self.vad.loudest,
                threshold=int(self.vad.threshold),
            )

    def _emit(self, item: BackendEvent | BaseException) -> None:
        self._events.put_nowait(item)

    def _start_pipeline(
        self,
        *,
        audio: bytes | None = None,
        text: str | None = None,
    ) -> None:
        if self._pipeline is not None and not self._pipeline.done():
            self._pipeline.cancel()

        self._pipeline = asyncio.create_task(
            self._run(self._turn, audio, text)
        )

    async def _stop_pipeline(self) -> None:
        task, self._pipeline = self._pipeline, None

        if task is None or task.done():
            return

        task.cancel()

        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(
        self,
        turn: int,
        audio: bytes | None,
        text: str | None,
    ) -> None:
        started = time.monotonic()
        timing: dict[str, int] = {}

        def mark(name: str) -> None:
            timing.setdefault(
                name,
                int((time.monotonic() - started) * 1000),
            )

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
                del self._history[:-self.HISTORY_MESSAGES]

            event(
                self.log,
                "turn_timing",
                tts_peak=int(self._tts_peak * 100),
                **timing,
            )

            if self._mode == "realtime":
                self.vad.reset()
                self._capturing = True

        except asyncio.CancelledError:
            raise

        except BackendError as exc:
            self._emit(exc)

        except Exception as exc:
            self._emit(
                BackendError(
                    f"Pipeline failed: {type(exc).__name__}"
                )
            )

    async def _respond(self, turn: int, text: str, mark) -> str:
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(
            self._speak(turn, sentences, mark)
        )

        kept: list[str] = []
        limit = self.max_sentences

        try:
            pending = ""

            async with aclosing(self._chat(text)) as deltas:
                async for delta in deltas:
                    mark("llm_first_ms")
                    pending += delta

                    parts = _SENTENCE_END.split(pending)
                    pending = parts[-1]

                    for sentence in parts[:-1]:
                        if not limit or len(kept) < limit:
                            self._keep(
                                turn,
                                sentence,
                                kept,
                                sentences,
                            )

                    if limit and len(kept) >= limit:
                        pending = ""
                        break

            self._keep(turn, pending, kept, sentences)

            self._emit(ResponseText(turn, "", final=True))
            mark("text_done_ms")

            sentences.put_nowait(None)
            await speaker

        finally:
            speaker.cancel()

        return _clean(" ".join(kept))

    def _keep(
        self,
        turn: int,
        sentence: str,
        kept: list[str],
        sentences: asyncio.Queue,
    ) -> None:
        sentence = _clean(sentence)

        if sentence:
            kept.append(sentence)
            self._emit(ResponseText(turn, sentence + " "))
            sentences.put_nowait(sentence)

    async def _speak(
        self,
        turn: int,
        sentences: asyncio.Queue,
        mark,
    ) -> None:
        streams: asyncio.Queue[asyncio.Queue | None] = asyncio.Queue()
        producers: list[asyncio.Task] = []

        async def produce(
            text: str,
            out: asyncio.Queue,
        ) -> None:
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
                    if sentences.empty() and not (
                        first
                        and len(" ".join(chunk)) < self.MIN_FIRST_CHARS
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
                    producers.append(
                        asyncio.create_task(produce(spoken, out))
                    )
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

    def _now(self) -> str:
        # Kept compatible with the existing configuration for now.
        if not getattr(self.config, "tell_date", False):
            return ""

        offset = timedelta(
            minutes=getattr(
                self.config,
                "timezone_offset_minutes",
                0,
            )
        )

        now = datetime.now(timezone(offset))

        return now.strftime(
            " Background information, never mention it unless the user asks about"
            " the date or time: it is now %A %Y-%m-%d %H:%M local time."
        )

    async def _transcribe(self, pcm16k: bytes) -> str:
        raise NotImplementedError

    async def _chat(self, text: str) -> AsyncIterator[str]:
        raise NotImplementedError

    async def _synthesise(
        self,
        text: str,
    ) -> AsyncIterator[bytes]:
        raise NotImplementedError
