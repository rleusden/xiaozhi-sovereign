from __future__ import annotations

import ctypes
import ctypes.util


class OpusError(RuntimeError):
    pass


class OpusCodec:
    INPUT_SAMPLES = 960
    OUTPUT_SAMPLES = 1440
    MAX_PACKET = 1275
    OPUS_APPLICATION_VOIP = 2048

    def __init__(self) -> None:
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        try:
            self._lib = ctypes.CDLL(name)
        except OSError as exc:
            raise OpusError("libopus could not be loaded") from exc
        self._bind()
        error = ctypes.c_int()
        self._decoder = self._lib.opus_decoder_create(16000, 1, ctypes.byref(error))
        self._check(error.value, "decoder create")
        self._encoder = self._lib.opus_encoder_create(
            24000, 1, self.OPUS_APPLICATION_VOIP, ctypes.byref(error)
        )
        self._check(error.value, "encoder create")

    def _bind(self) -> None:
        self._lib.opus_decoder_create.restype = ctypes.c_void_p
        self._lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self._lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._lib.opus_decode.restype = ctypes.c_int
        self._lib.opus_encoder_create.restype = ctypes.c_void_p
        self._lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self._lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        self._lib.opus_encode.restype = ctypes.c_int32
        self._lib.opus_strerror.argtypes = [ctypes.c_int]
        self._lib.opus_strerror.restype = ctypes.c_char_p

    def _check(self, code: int, operation: str) -> None:
        if code < 0:
            detail = self._lib.opus_strerror(code).decode("ascii", "replace")
            raise OpusError(f"Opus {operation} failed: {detail}")

    def decode_16k(self, packet: bytes) -> bytes:
        if not 0 < len(packet) <= self.MAX_PACKET:
            raise OpusError("invalid Opus packet size")
        source = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        pcm = (ctypes.c_int16 * self.INPUT_SAMPLES)()
        samples = self._lib.opus_decode(
            self._decoder, source, len(packet), pcm, self.INPUT_SAMPLES, 0
        )
        self._check(samples, "decode")
        if samples != self.INPUT_SAMPLES:
            raise OpusError(f"expected 960 decoded samples, got {samples}")
        return ctypes.string_at(pcm, samples * 2)

    def encode_24k(self, pcm: bytes) -> bytes:
        if len(pcm) != self.OUTPUT_SAMPLES * 2:
            raise OpusError("a downlink frame must contain 1440 PCM16 samples")
        source = (ctypes.c_int16 * self.OUTPUT_SAMPLES).from_buffer_copy(pcm)
        packet = (ctypes.c_ubyte * 4000)()
        size = self._lib.opus_encode(
            self._encoder, source, self.OUTPUT_SAMPLES, packet, len(packet)
        )
        self._check(size, "encode")
        return bytes(packet[:size])

    def close(self) -> None:
        if getattr(self, "_decoder", None):
            self._lib.opus_decoder_destroy(self._decoder)
            self._decoder = None
        if getattr(self, "_encoder", None):
            self._lib.opus_encoder_destroy(self._encoder)
            self._encoder = None

