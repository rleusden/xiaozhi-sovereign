import base64
import json
import unittest
from types import SimpleNamespace

from xiaozhi_gateway.backend import (
    BackendError,
    ResponseAudio,
    ResponseDone,
    ResponseStarted,
    ResponseText,
    UserSpeechEnded,
    UserSpeechStarted,
    UserTranscript,
    create_backend,
)
from xiaozhi_gateway.openai_backend import OpenAIBackend


class FakeUpstream:
    def __init__(self, incoming=()):
        self.sent = []
        self.incoming = [json.dumps(item) for item in incoming]

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self.incoming:
            yield item

    async def close(self):
        pass


def config():
    return SimpleNamespace(
        backend="openai",
        openai_api_key="test",
        openai_model="model",
        transcription_model="stt",
        voice="voice",
        instructions="instructions",
    )


def backend(incoming=()):
    instance = OpenAIBackend(config(), "device\0client")
    instance.ws = FakeUpstream(incoming)
    return instance, instance.ws


def kinds(upstream):
    return [message["type"] for message in upstream.sent]


def detection(message):
    return message["session"]["audio"]["input"]["turn_detection"]


class OpenAIBackendCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_selects_openai(self):
        self.assertIsInstance(create_backend(config(), "id"), OpenAIBackend)
        with self.assertRaises(BackendError):
            create_backend(SimpleNamespace(backend="other"), "id")

    async def test_auto_turn_enables_server_vad(self):
        instance, upstream = backend()
        await instance.start_turn(1, "auto")
        self.assertEqual(kinds(upstream), ["session.update"])
        self.assertEqual(detection(upstream.sent[0])["type"], "server_vad")

    async def test_manual_turn_resamples_and_commits(self):
        instance, upstream = backend()
        await instance.start_turn(1, "manual")
        await instance.append_audio(b"\0" * 1920)
        await instance.end_turn()
        self.assertEqual(
            kinds(upstream),
            [
                "session.update",
                "input_audio_buffer.append",
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
                "response.create",
            ],
        )
        self.assertIsNone(detection(upstream.sent[0]))
        sent = sum(
            len(base64.b64decode(message["audio"]))
            for message in upstream.sent
            if message["type"] == "input_audio_buffer.append"
        )
        # 960 samples at 16 kHz become 1440 samples at 24 kHz.
        self.assertEqual(sent, 1440 * 2)

    async def test_explicit_stop_in_auto_mode_disables_vad_before_commit(self):
        instance, upstream = backend()
        await instance.start_turn(1, "auto")
        await instance.append_audio(b"\0" * 1920)
        await instance.end_turn()
        self.assertEqual(
            kinds(upstream)[-3:],
            ["session.update", "input_audio_buffer.commit", "response.create"],
        )
        self.assertIsNone(detection(upstream.sent[-3]))

    async def test_text_turn_disables_vad_and_requests_response(self):
        instance, upstream = backend()
        await instance.send_text(3, "Hi XiaoZhi")
        self.assertEqual(
            kinds(upstream),
            ["session.update", "conversation.item.create", "response.create"],
        )
        self.assertIsNone(detection(upstream.sent[0]))
        self.assertEqual(
            upstream.sent[1]["item"]["content"][0]["text"], "Hi XiaoZhi"
        )

    async def test_cancel_maps_to_cancel_and_clear(self):
        instance, upstream = backend()
        await instance.cancel(response=True, clear_audio=True)
        await instance.cancel(response=False, clear_audio=False)
        self.assertEqual(
            kinds(upstream), ["response.cancel", "input_audio_buffer.clear"]
        )

    async def test_cancel_covers_a_response_the_session_did_not_see(self):
        incoming = [{"type": "response.created", "response": {"id": "r1"}}]
        instance, upstream = backend(incoming)
        await instance.start_turn(1, "auto")
        [_ async for _ in instance.events()]
        upstream.sent.clear()
        await instance.cancel(response=False, clear_audio=False)
        self.assertEqual(kinds(upstream), ["response.cancel"])


class OpenAIBackendEventTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, incoming, turn=1):
        instance, _ = backend(incoming)
        await instance.start_turn(turn, "auto")
        return [item async for item in instance.events()]

    async def test_response_events_are_normalised_and_tagged(self):
        pcm = b"\1\0" * 1440
        events = await self.collect(
            [
                {"type": "input_audio_buffer.speech_started"},
                {"type": "input_audio_buffer.speech_stopped"},
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Hoe laat is het?",
                },
                {"type": "response.created", "response": {"id": "r1"}},
                {
                    "type": "response.output_audio_transcript.delta",
                    "response_id": "r1",
                    "delta": "Hallo",
                },
                {
                    "type": "response.output_audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(pcm).decode(),
                },
                {"type": "response.done", "response": {"id": "r1"}},
                {"type": "rate_limits.updated"},
            ],
            turn=7,
        )
        self.assertEqual(
            events,
            [
                UserSpeechStarted(),
                UserSpeechEnded(),
                UserTranscript("Hoe laat is het?"),
                ResponseStarted(7),
                ResponseText(7, "Hallo"),
                ResponseAudio(7, pcm),
                ResponseDone(7),
            ],
        )

    async def test_output_of_unknown_response_is_dropped(self):
        events = await self.collect(
            [
                {"type": "response.output_audio.delta", "response_id": "x", "delta": "AAAA"},
                {"type": "response.done", "response": {"id": "x"}},
            ]
        )
        self.assertEqual(events, [])

    async def test_response_keeps_the_turn_it_started_in(self):
        instance, upstream = backend(
            [
                {"type": "response.created", "response": {"id": "r1"}},
            ]
        )
        await instance.start_turn(1, "auto")
        first = [item async for item in instance.events()]
        await instance.start_turn(2, "auto")
        upstream.incoming = [json.dumps({"type": "response.done", "response": {"id": "r1"}})]
        second = [item async for item in instance.events()]
        self.assertEqual(first + second, [ResponseStarted(1), ResponseDone(1)])

    async def test_provider_error_raises_without_payload(self):
        with self.assertRaises(BackendError) as caught:
            await self.collect(
                [{"type": "error", "error": {"message": "secret detail"}}]
            )
        self.assertNotIn("secret", str(caught.exception))

    async def test_response_without_id_is_an_error(self):
        with self.assertRaises(BackendError):
            await self.collect([{"type": "response.created", "response": {}}])


if __name__ == "__main__":
    unittest.main()
