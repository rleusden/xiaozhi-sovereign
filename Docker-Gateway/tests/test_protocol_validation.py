import asyncio
import json
import unittest
from types import SimpleNamespace

from xiaozhi_gateway.backend import (
    ResponseAudio,
    ResponseDone,
    ResponseStarted,
    ResponseText,
    UserTranscript,
)
from xiaozhi_gateway.protocol import DeviceSession, State


class FakeCodec:
    def decode_16k(self, packet):
        return b"\0" * 1920

    def encode_24k(self, pcm):
        return b"opus"

    def close(self):
        pass


class FakeBackend:
    def __init__(self, events=()):
        self.calls = []
        self.script = list(events)

    async def start_turn(self, turn, mode):
        self.calls.append(("start", turn, mode))

    async def append_audio(self, pcm):
        self.calls.append(("audio", len(pcm)))

    async def end_turn(self):
        self.calls.append(("end",))

    async def send_text(self, turn, text):
        self.calls.append(("text", turn, text))

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
        return DeviceSession(ws, config, backend=FakeBackend(), codec=FakeCodec())

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
        backend = FakeBackend(events)
        config = SimpleNamespace(max_turn_seconds=60)
        session = DeviceSession(ws, config, backend=backend, codec=FakeCodec())
        session.state = State.IDLE
        return session, ws, backend

    async def test_manual_turn_decodes_and_ends_turn(self):
        session, _, backend = self.session()
        await session._on_control({"type": "listen", "state": "start", "mode": "manual", "session_id": session.session_id})
        await session._on_audio(b"packet")
        await session._on_control({"type": "listen", "state": "stop", "session_id": session.session_id})
        self.assertEqual(session.state, State.PROCESSING)
        self.assertEqual(
            backend.calls, [("start", 1, "manual"), ("audio", 1920), ("end",)]
        )
        self.assertEqual(session.input_samples, 960)

    async def test_wakeword_preroll_is_bounded_and_discarded(self):
        session, _, backend = self.session()
        await session._on_audio(b"packet")
        self.assertEqual(session.prelisten_packets, 1)
        self.assertEqual(backend.calls, [])

    async def test_auto_trailing_audio_is_validated_and_discarded(self):
        session, _, backend = self.session()
        session.state = State.PROCESSING
        session.mode = "auto"
        await session._on_audio(b"packet")
        self.assertEqual(backend.calls, [])

    async def test_detect_then_start_coalesces_without_text_request(self):
        session, _, backend = self.session()
        await session._on_control(
            {"type": "listen", "state": "detect", "text": "Hi XiaoZhi", "session_id": session.session_id}
        )
        await session._on_control(
            {"type": "listen", "state": "start", "mode": "auto", "session_id": session.session_id}
        )
        await asyncio.sleep(0)
        self.assertEqual(backend.calls, [("start", 2, "auto")])
        self.assertEqual(session.state, State.LISTENING)

    async def test_abort_emits_exactly_one_stop(self):
        session, ws, backend = self.session()
        session.state = State.SPEAKING
        session.turn = 4
        session.tts_stopped = False
        await session._abort()
        stops = [
            json.loads(value)
            for value in ws.sent
            if isinstance(value, str) and json.loads(value).get("state") == "stop"
        ]
        self.assertEqual(len(stops), 1)
        self.assertIn(("cancel", True, False), backend.calls)

    async def test_detect_submits_text_after_grace(self):
        session, ws, backend = self.session()
        await session._on_control(
            {"type": "listen", "state": "detect", "text": "Hi XiaoZhi", "session_id": session.session_id}
        )
        await asyncio.sleep(0.3)
        self.assertEqual(backend.calls, [("text", 1, "Hi XiaoZhi")])
        self.assertEqual(json.loads(ws.sent[0])["type"], "stt")

    async def test_auto_stop_without_audio_does_not_end_turn(self):
        session, _, backend = self.session()
        await session._on_control({"type": "listen", "state": "start", "mode": "auto", "session_id": session.session_id})
        await session._on_control({"type": "listen", "state": "stop", "session_id": session.session_id})
        self.assertEqual(backend.calls, [("start", 1, "auto")])

    async def test_empty_manual_turn_is_rejected(self):
        session, _, _ = self.session()
        await session._on_control({"type": "listen", "state": "start", "mode": "manual", "session_id": session.session_id})
        with self.assertRaises(ValueError):
            await session._on_control({"type": "listen", "state": "stop", "session_id": session.session_id})

    async def test_response_of_superseded_turn_is_dropped(self):
        events = [
            ResponseStarted(1),
            ResponseAudio(1, b"\0" * 2880),
            ResponseDone(1),
        ]
        session, ws, _ = self.session(events)
        session.turn = 2
        await session._read_upstream()
        self.assertEqual(ws.sent, [])
        self.assertTrue(session.output_queue.empty())
        self.assertEqual(session.state, State.IDLE)

    async def play(self, events, *, user_text_sent=False):
        session, ws, _ = self.session(events)
        session.state = State.PROCESSING
        session.turn = 1
        session.user_text_sent = user_text_sent
        sender = asyncio.create_task(session._send_output())
        try:
            await session._read_upstream()
            await session.output_queue.join()
        finally:
            sender.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sender
        out = []
        for value in ws.sent:
            if isinstance(value, bytes):
                out.append("binary")
            else:
                message = json.loads(value)
                label = f"{message['type']}/{message.get('state', '')}"
                if "text" in message:
                    label += f":{message['text']}"
                out.append(label)
        return out

    async def test_late_user_transcript_is_shown_before_complete_answer(self):
        sent = await self.play(
            [
                ResponseStarted(1),
                ResponseText(1, "Op dit moment "),
                ResponseAudio(1, b"\0" * 2880),
                ResponseText(1, "wonen er 570.000 mensen."),
                ResponseDone(1),
                UserTranscript("Hoeveel mensen wonen er in Den Haag?"),
            ]
        )
        texts = [item for item in sent if ":" in item]
        self.assertEqual(
            texts,
            [
                "stt/:Hoeveel mensen wonen er in Den Haag?",
                "tts/sentence_start:Op dit moment wonen er 570.000 mensen.",
            ],
        )
        self.assertEqual(sent[0], "tts/start")
        self.assertEqual(sent[-1], "tts/stop")

    async def test_answer_text_is_sent_once_and_complete(self):
        sent = await self.play(
            [
                UserTranscript("Vraag"),
                ResponseStarted(1),
                ResponseText(1, "Een. "),
                ResponseAudio(1, b"\0" * 2880),
                ResponseText(1, "Twee."),
                ResponseDone(1),
            ]
        )
        self.assertEqual(
            sent,
            [
                "stt/:Vraag",
                "tts/start",
                "tts/sentence_start:Een. Twee.",
                "binary",
                "tts/stop",
            ],
        )

    async def test_answer_is_shown_at_stop_when_transcript_never_arrives(self):
        sent = await self.play(
            [
                ResponseStarted(1),
                ResponseText(1, "Hallo"),
                ResponseAudio(1, b"\0" * 2880),
                ResponseDone(1),
            ]
        )
        self.assertEqual(
            sent, ["tts/start", "binary", "tts/sentence_start:Hallo", "tts/stop"]
        )

    async def test_final_text_is_shown_before_the_audio_is_complete(self):
        sent = await self.play(
            [
                UserTranscript("Vraag"),
                ResponseStarted(1),
                ResponseText(1, "Een. "),
                ResponseText(1, "Twee."),
                ResponseText(1, "", final=True),
                ResponseAudio(1, b"\0" * 2880 * 3),
                ResponseDone(1),
            ]
        )
        self.assertEqual(
            sent,
            [
                "stt/:Vraag",
                "tts/start",
                "tts/sentence_start:Een. Twee.",
                "binary",
                "binary",
                "binary",
                "tts/stop",
            ],
        )

    async def test_long_answer_does_not_hold_up_later_events(self):
        # 10 s of audio is more than the old 64-frame queue could take; the
        # event after it must still be handled at once.
        session, ws, _ = self.session(
            [
                ResponseStarted(1),
                ResponseAudio(1, b"\0" * 2880 * 170),
                ResponseText(1, "Klaar.", final=True),
            ]
        )
        session.turn = 1
        session.user_text_sent = True
        await asyncio.wait_for(session._read_upstream(), 2)
        texts = [json.loads(v).get("text") for v in ws.sent if isinstance(v, str)]
        self.assertIn("Klaar.", texts)

    async def test_each_response_in_a_turn_gets_its_own_text(self):
        sent = await self.play(
            [
                ResponseStarted(1),
                ResponseText(1, "Eerste"),
                ResponseDone(1),
                ResponseStarted(1),
                ResponseText(1, "Tweede"),
                ResponseDone(1),
            ],
            user_text_sent=True,
        )
        self.assertEqual(
            [item for item in sent if ":" in item],
            ["tts/sentence_start:Eerste", "tts/sentence_start:Tweede"],
        )

    async def test_audio_order_is_start_sentence_binary_stop(self):
        events = [
            ResponseStarted(1),
            ResponseText(1, "Hallo"),
            ResponseAudio(1, b"\0" * 2880),
            ResponseDone(1),
        ]
        session, ws, _ = self.session(events)
        session.state = State.PROCESSING
        session.turn = 1
        session.user_text_sent = True
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
