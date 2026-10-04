import array
import unittest

from xiaozhi_gateway.resample import Resample16To24


class ResampleTests(unittest.TestCase):
    def test_streaming_ratio_is_exact_over_two_frames(self):
        source = array.array("h", range(1920)).tobytes()
        converter = Resample16To24()
        first = converter.process(source[: 960 * 2])
        second = converter.process(source[960 * 2 :])
        tail = converter.flush()
        self.assertEqual((len(first) + len(second) + len(tail)) // 2, 2880)

    def test_reset_restarts_phase(self):
        source = array.array("h", [100] * 960).tobytes()
        converter = Resample16To24()
        one = converter.process(source)
        converter.reset()
        two = converter.process(source)
        self.assertEqual(one, two)

    def test_odd_pcm_is_rejected(self):
        with self.assertRaises(ValueError):
            Resample16To24().process(b"x")


if __name__ == "__main__":
    unittest.main()
