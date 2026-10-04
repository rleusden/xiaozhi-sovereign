from __future__ import annotations

import array
import sys


class Resample16To24:
    """Streaming linear 16 kHz -> 24 kHz mono PCM16 resampler."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._previous: int | None = None
        self._index = -1
        self._next_num = 0  # output position, in thirds of an input sample

    def process(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise ValueError("PCM16 byte length must be even")
        values = array.array("h")
        values.frombytes(pcm)
        if sys.byteorder != "little":
            values.byteswap()
        out = array.array("h")
        for current in values:
            self._index += 1
            if self._previous is None:
                out.append(current)
                self._previous = current
                self._next_num = 2
                continue
            segment_start = 3 * (self._index - 1)
            segment_end = 3 * self._index
            while self._next_num <= segment_end:
                distance = self._next_num - segment_start
                sample = round(
                    (self._previous * (3 - distance) + current * distance) / 3
                )
                out.append(max(-32768, min(32767, sample)))
                self._next_num += 2
            self._previous = current
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    def flush(self) -> bytes:
        """Emit the final sample that needs a future point for interpolation."""
        if self._previous is None or self._next_num > 3 * self._index + 1:
            return b""
        out = array.array("h", [self._previous])
        if sys.byteorder != "little":
            out.byteswap()
        self._next_num += 2
        return out.tobytes()
