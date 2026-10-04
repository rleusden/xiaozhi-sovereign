import math
import struct
import unittest

from xiaozhi_gateway.vad import EnergyVad


def tone(ms, amplitude):
    count = 16 * ms
    return struct.pack(
        f"<{count}h",
        *[int(amplitude * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(count)],
    )


def feed(vad, pcm, step=1920):
    """Feed in 60 ms device packets and collect the state changes."""
    changes = []
    for start in range(0, len(pcm), step):
        change = vad.feed(pcm[start : start + step])
        if change:
            changes.append(change)
    return changes


class EnergyVadTests(unittest.TestCase):
    def test_silence_never_starts_a_turn(self):
        vad = EnergyVad()
        self.assertEqual(feed(vad, tone(3000, 40)), [])
        self.assertFalse(vad.in_speech)
        self.assertGreater(vad.loudest, 0)

    def test_speech_then_silence_gives_start_and_end(self):
        vad = EnergyVad(silence_ms=800)
        pcm = tone(600, 40) + tone(1200, 6000) + tone(1500, 40)
        self.assertEqual(feed(vad, pcm), ["start", "end"])

    def test_end_needs_the_configured_silence(self):
        vad = EnergyVad(silence_ms=800)
        pcm = tone(300, 40) + tone(900, 6000) + tone(600, 40) + tone(600, 6000)
        self.assertEqual(feed(vad, pcm), ["start"])
        self.assertEqual(feed(vad, tone(900, 40)), ["end"])

    def test_captured_speech_has_preroll_and_a_short_tail(self):
        vad = EnergyVad(silence_ms=800)
        feed(vad, tone(600, 40) + tone(1200, 6000) + tone(1500, 40))
        ms = len(vad.take_speech()) // 32
        # 1200 ms of speech, at most 300 ms before it and 200 ms after it.
        self.assertGreaterEqual(ms, 1200)
        self.assertLessEqual(ms, 1200 + 300 + 200 + 60)
        self.assertFalse(vad.in_speech)

    def test_short_click_is_not_speech(self):
        vad = EnergyVad()
        self.assertEqual(feed(vad, tone(500, 40) + tone(40, 9000) + tone(500, 40)), [])

    def test_threshold_follows_background_noise(self):
        vad = EnergyVad(threshold=300)
        # Steady background noise below the base threshold raises the
        # threshold to three times the measured noise level.
        self.assertEqual(feed(vad, tone(3000, 250)), [])
        self.assertGreater(vad.threshold, 450)
        self.assertEqual(feed(vad, tone(600, 6000)), ["start"])

    def test_reset_clears_state(self):
        vad = EnergyVad()
        feed(vad, tone(600, 6000))
        vad.reset()
        self.assertFalse(vad.in_speech)
        self.assertEqual(vad.take_speech(), b"")


if __name__ == "__main__":
    unittest.main()
