import asyncio
import base64
import json
import unittest
from types import SimpleNamespace

from xiaozhi_gateway.protocol import DeviceSession, State


class FakeCodec:
    def decode_16k(self, packet):
        return b"\0" * 1920

    def encode_24k(self, pcm):
        return b"opus"

    def close(self):
        pass


class FakeRealtime:
    def __init__(self, events=()):
        self.calls = []
        self.script = list(events)

    async def set_mode(self, mode):
        self.calls.append(("mode", mode))

    async def append_pcm(self, pcm):
        self.calls.append(("audio", len(pcm)))

    async def commit_and_respond(self):
        self.calls.append(("commit",))

    async def send_text(self, text):
        self.calls.append(("text", text))

    async def cancel(self, *, response, clear_audio):
        self.calls.append(("cancel", response, clear_audio))

    async def events(self):
        for event in self.script:
            yield event

    async def close(self):
        pass


class FakeWebSocket:
    def __init__(self):
        self.request = SimpleNamespace(headers={}, path="/xiaozhi/ws")
        self.sent = []

    async def send(self, value):
        self.sent.append(value)


class ProtocolValidationTests(unittest.TestCase):
    def session(self):
        ws = FakeWebSocket()
        config = SimpleNamespace()
        return DeviceSession(ws, config, realtime=FakeRealtime(), codec=FakeCodec())

    def test_exact_client_audio_contract(self):
        session = self.session()
        hello = {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        session._validate_hello(hello)

    def test_wrong_sample_rate_is_rejected(self):
        session = self.session()
        hello = {
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 24000,
                "channels": 1,
                "frame_duration": 60,
            },
        }
        with self.assertRaises(ValueError):
            session._validate_hello(hello)

    def test_binary_is_not_json(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session._parse_json(b"{}")


class ProtocolFlowTests(unittest.IsolatedAsyncioTestCase):
    def session(self, events=()):
        ws = FakeWebSocket()
        realtime = FakeRealtime(events)
        config = SimpleNamespace(max_turn_seconds=60)
        session = DeviceSession(ws, config, realtime=realtime, codec=FakeCodec())
        session.state = State.IDLE
        return session, ws, realtime

    async def test_manual_turn_decodes_resamples_and_commits(self):
        session, _, realtime = self.session()
        await session._on_control({"type": "listen", "state": "start", "mode": "manual", "session_id": session.session_id})
        await session._on_audio(b"packet")
        await session._on_control({"type": "listen", "state": "stop", "session_id": session.session_id})
        self.assertEqual(session.state, State.PROCESSING)
        self.assertIn(("mode", "manual"), realtime.calls)
        self.assertIn(("commit",), realtime.calls)
        self.assertEqual(session.input_samples, 960)

    async def test_wakeword_preroll_is_bounded_and_discarded(self):
        session, _, realtime = self.session()
        await session._on_audio(b"packet")
        self.assertEqual(session.prelisten_packets, 1)
        self.assertEqual(realtime.calls, [])

    async def test_auto_trailing_audio_is_validated_and_discarded(self):
        session, _, realtime = self.session()
        session.state = State.PROCESSING
        session.mode = "auto"
        await session._on_audio(b"packet")
        self.assertEqual(realtime.calls, [])

    async def test_detect_then_start_coalesces_without_text_request(self):
        session, _, realtime = self.session()
        await session._on_control(
            {"type": "listen", "state": "detect", "text": "Hi XiaoZhi", "session_id": session.session_id}
        )
        await session._on_control(
            {"type": "listen", "state": "start", "mode": "auto", "session_id": session.session_id}
        )
        await asyncio.sleep(0)
        self.assertNotIn(("text", "Hi XiaoZhi"), realtime.calls)
        self.assertEqual(session.state, State.LISTENING)

    async def test_abort_emits_exactly_one_stop(self):
        session, ws, realtime = self.session()
        session.state = State.SPEAKING
        session.turn = 4
        session.responses["r1"] = 4
        session.tts_stopped = False
        await session._abort()
        stops = [
            json.loads(value)
            for value in ws.sent
            if isinstance(value, str) and json.loads(value).get("state") == "stop"
        ]
        self.assertEqual(len(stops), 1)
        self.assertIn(("cancel", True, False), realtime.calls)

    async def test_openai_audio_order_is_start_sentence_binary_stop(self):
        pcm = base64.b64encode(b"\0" * 2880).decode()
        events = [
            {"type": "response.created", "response": {"id": "r1"}},
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": "r1",
                "delta": "Hallo",
            },
            {"type": "response.output_audio.delta", "response_id": "r1", "delta": pcm},
            {"type": "response.done", "response": {"id": "r1"}},
        ]
        session, ws, _ = self.session(events)
        session.state = State.PROCESSING
        session.turn = 1
        sender = asyncio.create_task(session._send_output())
        try:
            await session._read_upstream()
            await session.output_queue.join()
        finally:
            sender.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sender
        kinds = []
        for value in ws.sent:
            if isinstance(value, bytes):
                kinds.append("binary")
            else:
                message = json.loads(value)
                kinds.append(f"{message['type']}/{message.get('state', '')}")
        self.assertEqual(
            kinds,
            ["tts/start", "tts/sentence_start", "binary", "tts/stop"],
        )


if __name__ == "__main__":
    unittest.main()
