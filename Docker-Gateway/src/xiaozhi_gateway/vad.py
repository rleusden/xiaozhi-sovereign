from __future__ import annotations

import array
import sys
from collections import deque


class EnergyVad:
    """Small energy-based end-of-turn detector for 16 kHz mono PCM16.

    It is deliberately simple: a frame counts as speech when its RMS level is
    above a threshold that adapts to the measured background noise. Speech
    starts after a short run of speech frames and ends after ``silence_ms`` of
    non-speech. ``feed`` returns "start", "end" or None for each call.
    """

    RATE = 16000
    FRAME_MS = 20
    FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
    START_FRAMES = 4  # 80 ms of speech starts a turn
    PREROLL_MS = 300
    TAIL_MS = 200
    NOISE_RATIO = 3.0
    RELEASE = 0.6  # hysteresis: stay in speech above 60 % of the threshold

    def __init__(self, *, threshold: int = 300, silence_ms: int = 800) -> None:
        self.base_threshold = threshold
        self.silence_frames = max(1, silence_ms // self.FRAME_MS)
        self.reset()

    def reset(self) -> None:
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=self.PREROLL_MS // self.FRAME_MS)
        self._speech = bytearray()
        self._noise = 0.0
        self._run = 0
        self._silence = 0
        self.in_speech = False
        self.ended = False
        self.peak = 0
        self.loudest = 0  # loudest frame since reset, speech or not

    @property
    def threshold(self) -> float:
        return max(self.base_threshold, self._noise * self.NOISE_RATIO)

    def feed(self, pcm: bytes) -> str | None:
        """Consume PCM and report the first state change, if any."""
        if self.ended:
            return None  # wait for take_speech() or reset()
        self._pending.extend(pcm)
        result = None
        while len(self._pending) >= self.FRAME_BYTES:
            frame = bytes(self._pending[: self.FRAME_BYTES])
            del self._pending[: self.FRAME_BYTES]
            change = self._frame(frame)
            if change == "end":
                self._pending.clear()
                self.ended = True
                return "end"
            result = result or change
        return result

    def _frame(self, frame: bytes) -> str | None:
        level = _rms(frame)
        self.loudest = max(self.loudest, level)
        if not self.in_speech:
            self._preroll.append(frame)
            if level > self.threshold:
                self._run += 1
                if self._run >= self.START_FRAMES:
                    self.in_speech = True
                    self._silence = 0
                    self.peak = level
                    self._speech = bytearray(b"".join(self._preroll))
                    self._preroll.clear()
                    return "start"
            else:
                self._run = 0
                self._noise += 0.05 * (level - self._noise)
            return None
        self._speech.extend(frame)
        self.peak = max(self.peak, level)
        if level > self.threshold * self.RELEASE:
            self._silence = 0
            return None
        self._silence += 1
        if self._silence >= self.silence_frames:
            return "end"
        return None

    def take_speech(self) -> bytes:
        """Return the captured utterance without most of its trailing silence."""
        keep = self.TAIL_MS // self.FRAME_MS
        drop = max(0, self._silence - keep) * self.FRAME_BYTES
        speech = bytes(self._speech[: len(self._speech) - drop])
        self._speech = bytearray()
        self.in_speech = False
        self.ended = False
        self._run = 0
        self._silence = 0
        return speech


def _rms(frame: bytes) -> int:
    samples = array.array("h")
    samples.frombytes(frame)
    if sys.byteorder != "little":
        samples.byteswap()
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5)
