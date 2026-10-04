import asyncio
import json
import struct
import sys
import threading
import unittest
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from fake_mistral import FakeMistral  # noqa: E402
from test_vad import tone  # noqa: E402

from xiaozhi_gateway.backend import (  # noqa: E402
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
from xiaozhi_gateway.http_client import HttpClient, HttpError  # noqa: E402
from xiaozhi_gateway.mistral_backend import MistralBackend  # noqa: E402

SPEECH = tone(300, 40) + tone(1200, 6000) + tone(1200, 40)


def config(fake, **changes):
    values = dict(
        backend="mistral",
        instructions="Antwoord kort.",
        mistral_api_key="test-key",
        mistral_base_url=fake.url,
        mistral_model="chat-model",
        mistral_stt_model="stt-model",
        mistral_tts_model="tts-model",
        mistral_voice_id="voice-x",
        mistral_language="nl",
        vad_threshold=300,
        vad_silence_ms=800,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class MistralCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeMistral()
        self.addCleanup(self.fake.close)

    async def backend(self, **changes):
        instance = create_backend(config(self.fake, **changes), "id")
        self.addAsyncCleanup(instance.close)
        await instance.open()
        return instance

    async def until_done(self, instance, timeout=5):
        events = []

        async def read():
            async for item in instance.events():
                events.append(item)
                if isinstance(item, ResponseDone):
                    return

        await asyncio.wait_for(read(), timeout)
        return events

    async def say(self, instance, turn, pcm=SPEECH, mode="auto"):
        await instance.start_turn(turn, mode)
        for start in range(0, len(pcm), 1920):
            await instance.append_audio(pcm[start : start + 1920])


class MistralTurnTests(MistralCase):
    async def test_factory_selects_mistral(self):
        self.assertIsInstance(await self.backend(), MistralBackend)

    async def test_auto_turn_runs_stt_chat_and_tts(self):
        instance = await self.backend()
        await self.say(instance, 4)
        events = await self.until_done(instance)

        self.assertEqual(
            [type(item) for item in events if not isinstance(item, (ResponseText, ResponseAudio))],
            [UserSpeechStarted, UserSpeechEnded, UserTranscript, ResponseStarted, ResponseDone],
        )
        self.assertEqual(events[2], UserTranscript("Hoeveel mensen wonen er in Den Haag?"))
        self.assertEqual(events[3], ResponseStarted(4))
        self.assertEqual(events[-1], ResponseDone(4))
        text = "".join(item.text for item in events if isinstance(item, ResponseText))
        # Markup is removed because the text is spoken and shown as plain text.
        self.assertEqual(
            text.strip(), "In Den Haag wonen ongeveer 550.000 mensen. Dat is veel!"
        )
        self.assertTrue(all(item.turn == 4 for item in events if hasattr(item, "turn")))

    async def test_requests_match_the_documented_api(self):
        instance = await self.backend()
        await self.say(instance, 1)
        await self.until_done(instance)

        stt = self.fake.bodies("/v1/audio/transcriptions")[0]
        self.assertEqual(stt["model"], b"stt-model")
        self.assertEqual(stt["language"], b"nl")
        with wave.open(BytesIO(stt["file"])) as audio:
            self.assertEqual(
                (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                (16000, 1, 2),
            )
            seconds = audio.getnframes() / 16000
        # The speech with a little context, not the whole listening period.
        self.assertGreater(seconds, 1.2)
        self.assertLess(seconds, 1.8)

        chat = self.fake.bodies("/v1/chat/completions")[0]
        self.assertEqual(chat["model"], "chat-model")
        self.assertTrue(chat["stream"])
        self.assertEqual([m["role"] for m in chat["messages"]], ["system", "user"])
        self.assertTrue(chat["messages"][0]["content"].startswith("Antwoord kort."))
        self.assertEqual(
            chat["messages"][1]["content"], "Hoeveel mensen wonen er in Den Haag?"
        )

        speech = self.fake.bodies("/v1/audio/speech")
        self.assertEqual(
            {k: v for k, v in speech[0].items() if k != "input"},
            {"model": "tts-model", "voice_id": "voice-x", "response_format": "pcm", "stream": True},
        )
        self.assertEqual(
            " ".join(item["input"] for item in speech),
            "In Den Haag wonen ongeveer 550.000 mensen. Dat is veel!",
        )

    async def test_float32_audio_becomes_pcm16(self):
        instance = await self.backend()
        await self.say(instance, 1)
        events = await self.until_done(instance)
        pcm = b"".join(item.pcm for item in events if isinstance(item, ResponseAudio))
        self.assertEqual(len(pcm) % 2, 0)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        # 0.5 -> 16383; out-of-range values are clipped and NaN becomes silence.
        self.assertEqual(samples[0], 16383)
        self.assertEqual(samples[-3:], (32767, -32767, 0))
        words = 10  # words in the spoken answer
        requests = len(self.fake.bodies("/v1/audio/speech"))
        self.assertEqual(len(samples), 2400 * words + 3 * requests)

    async def test_answer_is_cut_after_the_sentence_limit(self):
        self.fake.answer = ["Een. Twee", " is dit. ", "Drie volgt.", " Vier ook. Vijf."]
        instance = await self.backend(mistral_max_sentences=2)
        await instance.send_text(1, "Tel")
        events = await self.until_done(instance)
        text = "".join(item.text for item in events if isinstance(item, ResponseText))
        self.assertEqual(text.strip(), "Een. Twee is dit.")
        spoken = " ".join(item["input"] for item in self.fake.bodies("/v1/audio/speech"))
        self.assertEqual(spoken, "Een. Twee is dit.")
        # Only what was spoken is remembered.
        await instance.send_text(2, "Verder")
        await self.until_done(instance)
        history = self.fake.bodies("/v1/chat/completions")[-1]["messages"]
        self.assertEqual(history[2], {"role": "assistant", "content": "Een. Twee is dit."})

    async def test_sentence_limit_zero_keeps_the_whole_answer(self):
        self.fake.answer = ["Een. Twee. Drie. Vier. Vijf."]
        instance = await self.backend(mistral_max_sentences=0)
        await instance.send_text(1, "Tel")
        events = await self.until_done(instance)
        text = "".join(item.text for item in events if isinstance(item, ResponseText))
        self.assertEqual(text.strip(), "Een. Twee. Drie. Vier. Vijf.")

    async def test_text_is_complete_before_the_audio(self):
        instance = await self.backend()
        await instance.send_text(1, "Hallo")
        events = await self.until_done(instance)
        final = next(i for i, item in enumerate(events) if isinstance(item, ResponseText) and item.final)
        last_audio = max(i for i, item in enumerate(events) if isinstance(item, ResponseAudio))
        self.assertLess(final, last_audio)
        self.assertEqual(sum(1 for item in events if isinstance(item, ResponseText) and item.final), 1)

    async def test_model_is_told_the_local_date(self):
        instance = await self.backend(timezone_offset_minutes=120, mistral_tell_date=True)
        await instance.send_text(1, "Hallo")
        await self.until_done(instance)
        system = self.fake.bodies("/v1/chat/completions")[0]["messages"][0]["content"]
        self.assertRegex(system, r"it is now \w+ \d{4}-\d{2}-\d{2} \d{2}:\d{2} local time\.$")
        self.assertIn("never mention it unless the user asks", system)

    async def test_date_is_not_given_by_default(self):
        instance = await self.backend(timezone_offset_minutes=120)
        await instance.send_text(1, "Hallo")
        await self.until_done(instance)
        system = self.fake.bodies("/v1/chat/completions")[0]["messages"][0]["content"]
        self.assertNotIn("it is now", system)

    async def test_later_speech_is_requested_while_the_first_part_arrives(self):
        # The fake holds every speech request until the gate opens. Both parts
        # must have been requested by then, not one after the other.
        self.fake.answer = [
            "Dit is een eerste zin van voldoende lengte om mee te beginnen. ",
            "Dit is de tweede zin. ",
            "En dit is de derde.",
        ]
        self.fake.chat_delay = 0.1
        self.fake.speech_gate = threading.Event()
        instance = await self.backend()
        await instance.send_text(1, "Vertel")
        for _ in range(500):
            if len(self.fake.bodies("/v1/audio/speech")) >= 2:
                break
            await asyncio.sleep(0.01)
        requested = [item["input"] for item in self.fake.bodies("/v1/audio/speech")]
        self.assertGreaterEqual(len(requested), 2)
        self.fake.speech_gate.set()
        events = await self.until_done(instance)
        requested = [item["input"] for item in self.fake.bodies("/v1/audio/speech")]
        self.assertEqual(
            " ".join(requested),
            "Dit is een eerste zin van voldoende lengte om mee te beginnen. "
            "Dit is de tweede zin. En dit is de derde.",
        )
        pcm = b"".join(item.pcm for item in events if isinstance(item, ResponseAudio))
        # Audio of all parts, in order and complete: 0.1 s for each of the 22
        # words plus the three marker samples that end each request.
        self.assertEqual(len(pcm) // 2, 2400 * 22 + 3 * len(requested))

    async def test_short_opening_is_spoken_together_with_what_follows(self):
        self.fake.answer = ["Ja. ", "Dat klopt helemaal, de zon gaat vandaag vroeg onder. ", "Meer weet ik niet."]
        instance = await self.backend()
        await instance.send_text(1, "Klopt dat?")
        await self.until_done(instance)
        requested = [item["input"] for item in self.fake.bodies("/v1/audio/speech")]
        self.assertTrue(requested[0].startswith("Ja. Dat klopt helemaal"))

    async def test_gain_raises_the_volume_and_clips(self):
        instance = await self.backend(mistral_tts_gain=1.5)
        with self.assertLogs("xiaozhi.mistral", "INFO") as logs:
            await instance.send_text(1, "Hallo")
            events = await self.until_done(instance)
        pcm = b"".join(item.pcm for item in events if isinstance(item, ResponseAudio))
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        self.assertEqual(samples[0], int(32767 * 0.75))  # 0.5 * 1.5
        self.assertEqual(samples[-3:], (32767, -32767, 0))
        timing = [json.loads(line.split(":", 2)[2]) for line in logs.output if "turn_timing" in line]
        self.assertEqual(timing[0]["tts_peak"], 100)

    async def test_history_is_sent_on_the_next_turn(self):
        instance = await self.backend()
        await self.say(instance, 1)
        await self.until_done(instance)
        await self.say(instance, 2)
        await self.until_done(instance)
        second = self.fake.bodies("/v1/chat/completions")[1]["messages"]
        self.assertEqual(
            [m["role"] for m in second], ["system", "user", "assistant", "user"]
        )
        self.assertEqual(
            second[2]["content"], "In Den Haag wonen ongeveer 550.000 mensen. Dat is veel!"
        )

    async def test_manual_turn_waits_for_end_turn(self):
        instance = await self.backend()
        await self.say(instance, 1, pcm=tone(900, 6000), mode="manual")
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.paths(), [])
        await instance.end_turn()
        events = await self.until_done(instance)
        self.assertIsInstance(events[0], UserTranscript)
        with wave.open(BytesIO(self.fake.bodies("/v1/audio/transcriptions")[0]["file"])) as audio:
            self.assertEqual(audio.getnframes(), 16 * 900)

    async def test_text_turn_skips_transcription(self):
        instance = await self.backend()
        await instance.send_text(9, "Hi XiaoZhi")
        events = await self.until_done(instance)
        self.assertEqual(events[0], ResponseStarted(9))
        self.assertNotIn("/v1/audio/transcriptions", self.fake.paths())
        self.assertEqual(
            self.fake.bodies("/v1/chat/completions")[0]["messages"][-1]["content"],
            "Hi XiaoZhi",
        )

    async def test_unintelligible_turn_ends_with_an_empty_response(self):
        self.fake.transcript = ""
        instance = await self.backend()
        await self.say(instance, 1)
        events = await self.until_done(instance)
        self.assertEqual(
            events[-2:], [ResponseStarted(1), ResponseDone(1)]
        )
        self.assertNotIn("/v1/chat/completions", self.fake.paths())

    async def test_audio_after_the_turn_ended_is_ignored(self):
        instance = await self.backend()
        await self.say(instance, 1)
        await instance.append_audio(tone(600, 6000))
        await self.until_done(instance)
        self.assertEqual(len(self.fake.bodies("/v1/audio/transcriptions")), 1)

    async def test_cancel_stops_the_response(self):
        self.fake.speech_gate = threading.Event()
        instance = await self.backend()
        await self.say(instance, 1)
        while "/v1/audio/speech" not in self.fake.paths():
            await asyncio.sleep(0.01)
        await instance.cancel(response=True, clear_audio=False)
        self.fake.speech_gate.set()
        await asyncio.sleep(0.2)
        leftover = []
        while not instance._events.empty():
            leftover.append(instance._events.get_nowait())
        self.assertFalse(any(isinstance(item, (ResponseAudio, ResponseDone)) for item in leftover))
        # A cancelled exchange is not remembered.
        await instance.send_text(2, "Opnieuw")
        await self.until_done(instance)
        messages = self.fake.bodies("/v1/chat/completions")[-1]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])


class MistralFailureTests(MistralCase):
    async def test_provider_error_is_reported_without_payload(self):
        self.fake.status["/v1/audio/speech"] = 403
        instance = await self.backend()
        await self.say(instance, 1)
        with self.assertRaises(BackendError) as caught:
            await self.until_done(instance)
        self.assertEqual(str(caught.exception), "Mistral speech failed: HTTP 403")

    async def test_rate_limited_requests_are_retried(self):
        MistralBackend.RATE_LIMIT_DELAYS = (0.05, 0.05, 0.05)
        self.addCleanup(setattr, MistralBackend, "RATE_LIMIT_DELAYS", (1.1, 2.0, 3.0))
        self.fake.refuse = {
            "/v1/audio/transcriptions": 1,
            "/v1/chat/completions": 2,
            "/v1/audio/speech": 1,
        }
        instance = await self.backend()
        await self.say(instance, 1)
        events = await self.until_done(instance)
        text = "".join(item.text for item in events if isinstance(item, ResponseText))
        self.assertEqual(
            text.strip(), "In Den Haag wonen ongeveer 550.000 mensen. Dat is veel!"
        )
        self.assertTrue(any(isinstance(item, ResponseAudio) for item in events))
        self.assertEqual(self.fake.paths().count("/v1/chat/completions"), 3)

    async def test_retry_after_header_sets_the_wait(self):
        self.fake.retry_after = "0.3"
        self.fake.refuse = {"/v1/chat/completions": 1}
        instance = await self.backend()
        with self.assertLogs("xiaozhi.mistral", "INFO") as logs:
            await instance.send_text(1, "Hallo")
            await self.until_done(instance)
        limited = [json.loads(line.split(":", 2)[2]) for line in logs.output if "rate_limited" in line]
        self.assertEqual(
            [(item["stage"], item["wait_ms"]) for item in limited], [("chat", 300)]
        )

    async def test_persistent_rate_limit_is_reported(self):
        MistralBackend.RATE_LIMIT_DELAYS = (0.02, 0.02, 0.02)
        self.addCleanup(setattr, MistralBackend, "RATE_LIMIT_DELAYS", (1.1, 2.0, 3.0))
        self.fake.refuse = {"/v1/chat/completions": 99}
        instance = await self.backend()
        await instance.send_text(1, "Hallo")
        with self.assertRaises(BackendError) as caught:
            await self.until_done(instance)
        self.assertEqual(str(caught.exception), "Mistral chat failed: HTTP 429")
        self.assertEqual(self.fake.paths().count("/v1/chat/completions"), 4)

    async def test_wrong_key_is_reported_as_401(self):
        instance = await self.backend(mistral_api_key="wrong")
        await self.say(instance, 1)
        with self.assertRaises(BackendError) as caught:
            await self.until_done(instance)
        self.assertEqual(str(caught.exception), "Mistral transcription failed: HTTP 401")

    async def test_voice_for_the_language_is_chosen_when_none_is_set(self):
        instance = await self.backend(mistral_voice_id=None)
        self.assertEqual(instance.voice_id, "voice-nl")
        self.assertIn("type=preset", self.fake.paths()[0])

    async def test_missing_voice_for_the_language_is_an_error(self):
        self.fake.voices = self.fake.voices[:1]
        instance = create_backend(config(self.fake, mistral_voice_id=None), "id")
        self.addAsyncCleanup(instance.close)
        with self.assertRaises(BackendError) as caught:
            await instance.open()
        self.assertIn("MISTRAL_VOICE_ID", str(caught.exception))


class HttpClientTests(MistralCase):
    async def test_connections_are_reused(self):
        client = HttpClient(self.fake.url, {"Authorization": "Bearer test-key"})
        self.addCleanup(client.close)
        for _ in range(3):
            json.loads(await client.request("GET", "/v1/audio/voices"))
            lines = [
                line
                async for line in client.stream_lines(
                    "/v1/chat/completions",
                    body=b'{"messages": []}',
                    headers={"Content-Type": "application/json"},
                )
            ]
            self.assertIn(b"data: [DONE]\n", lines)
        self.assertEqual(self.fake.connections, 1)

    async def test_error_status_raises_without_body(self):
        client = HttpClient(self.fake.url, {})
        self.addCleanup(client.close)
        with self.assertRaises(HttpError) as caught:
            await client.request("POST", "/v1/audio/speech", body=b"{}")
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("secret", str(caught.exception))

    async def test_only_http_urls_are_accepted(self):
        with self.assertRaises(ValueError):
            HttpClient("ftp://example.org", {})


if __name__ == "__main__":
    unittest.main()
