import array
import unittest

from xiaozhi_gateway.opus import OpusCodec


class OpusTests(unittest.TestCase):
    def test_encode_produces_bounded_packet(self):
        codec = OpusCodec()
        try:
            pcm = array.array("h", [0] * 1440).tobytes()
            packet = codec.encode_24k(pcm)
            self.assertGreater(len(packet), 0)
            self.assertLessEqual(len(packet), codec.MAX_PACKET)
        finally:
            codec.close()


if __name__ == "__main__":
    unittest.main()
