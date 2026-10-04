# Mistral backend

`AI_BACKEND=mistral` replaces the single OpenAI Realtime session with three
Mistral API calls per turn:

```text
device audio (16 kHz) -> silence detection in the gateway
  -> POST /v1/audio/transcriptions   (Voxtral, whole utterance as WAV)
  -> POST /v1/chat/completions       (streamed)
  -> POST /v1/audio/speech           (Voxtral TTS, streamed float32 PCM)
-> device audio (24 kHz Opus)
```

Audio, transcripts and prompts are processed by Mistral AI (France) instead of
OpenAI. They still leave the home network; this is not a local setup.

## Status

Exercised on the reference deployment (Freenove board, Synology DS218+) on
2026-10-04, in English, with `open-mistral-nemo`, `voxtral-mini-latest`,
`voxtral-mini-tts-2603` and a British English preset voice: transcription,
answer, speech, follow-up turns and the on-screen text all worked. Streamed
`pcm` speech is 24 kHz mono float32, as assumed.

Known limits found in that test:

- **No Dutch voice.** The preset voices are US English, British English and
  French only. A British voice reading a Dutch answer is not intelligible.
  Mistral recommends a voice in the language of the text; a custom voice made
  from a Dutch sample (`POST /v1/audio/voices`) has not been tried.
- **Model availability depends on the API plan.** On the plan used for the
  test, `mistral-small-latest` and `mistral-medium-latest` answered HTTP 429
  with a request limit of zero, while `open-mistral-nemo` and
  `ministral-8b-latest` worked. Set `MISTRAL_MODEL` to a model your plan
  allows.
- **Speech is quiet.** Its peak was 16 to 33 percent of full scale; see
  [Volume](#volume).

The automated tests run against a local stand-in for the Mistral API
(`tests/fake_mistral.py`), not against the live service.

## Setup

1. Create the key file next to the OpenAI one. Compose mounts both files, so
   both must exist; only the key of the selected backend is read.

   ```sh
   umask 077
   printf '%s' 'REPLACE-WITH-MISTRAL-KEY' > secrets/mistral_api_key.txt
   sudo chown 65532:65532 secrets/mistral_api_key.txt
   sudo chmod 0400 secrets/mistral_api_key.txt
   ```

2. Set `AI_BACKEND=mistral` in `.env`, rebuild and recreate the container.

3. Choose a voice. With `MISTRAL_VOICE_ID` empty the gateway uses the first
   preset voice for `MISTRAL_LANGUAGE`, and fails to start a session when
   there is none, as is the case for `nl`. To list the voices:

   ```sh
   sudo docker compose run --rm --no-deps \
     --entrypoint /opt/venv/bin/python gateway -m xiaozhi_gateway.mistral_voices
   ```

Switching back is `AI_BACKEND=openai` and recreating the container.

## Turn detection

OpenAI detects the end of a turn on its side. Mistral does not, so the gateway
does it with a small energy detector (`vad.py`): speech starts after 80 ms
above the threshold and the turn ends after `VAD_SILENCE_MS` of silence. The
threshold is `VAD_THRESHOLD` or three times the measured background noise,
whichever is higher.

Two log events help with tuning:

| Event | Meaning |
|---|---|
| `speech_end` with `ms`, `peak`, `threshold` | a turn was detected; `peak` is the loudest speech level |
| `no_speech` with `loudest`, `threshold` | a listening period ended without speech |

- The device never reacts and `no_speech` shows `loudest` below `threshold`:
  lower `VAD_THRESHOLD`.
- Turns start on background noise: raise `VAD_THRESHOLD`.
- You are cut off while pausing: raise `VAD_SILENCE_MS`. The answer feels
  slow to start: lower it.

## Latency

`turn_timing` logs milliseconds from the end of the user's speech:
`stt_ms` (transcript ready), `llm_first_ms` (first text), `tts_first_ms`
(first audio) and `done_ms`. Add `VAD_SILENCE_MS` for the delay the user
experiences. The first sentence is spoken as soon as it is written; the rest
of the answer is synthesised while the first sentence plays.

## Answer length

The instruction asks the model for short answers, but a model does not
reliably obey that. `MISTRAL_MAX_SENTENCES` (default 3, `0` for no limit) is
enforced by the gateway: after that many sentences it stops reading the
model's answer, so the rest is not generated, spoken, shown or remembered.

## Date and time

The model does not know what day it is. `MISTRAL_TELL_DATE=1` adds the local
date and time (from `TIMEZONE_OFFSET_MINUTES`) to the instruction, marked as
background information. It is off by default because a small model may read
the date out at the start of every answer.

## Volume

Mistral speech can be quieter than the OpenAI voice. `MISTRAL_TTS_GAIN`
multiplies the audio before it is sent to the device; samples that would
exceed full scale are clipped. `turn_timing` logs `tts_peak`, the loudest
sample Mistral delivered as a percentage of full scale before the gain. A
gain of about `90 / tts_peak` uses the available range without clipping.

## Rate limits

One turn makes three or four requests within a second or two. Mistral answers
HTTP 429 when a limit of the API plan is hit; the free plan in particular
allows few requests per second. The gateway then waits (the `Retry-After`
value, otherwise 1.1, 2 and 3 seconds) and retries, logging `rate_limited`
with the stage and the wait. Each retry adds that wait to the answer time, so
a plan with a higher requests-per-second limit is needed for fluent use. After
three retries the turn fails with `Mistral <stage> failed: HTTP 429`.

## Differences from the OpenAI backend

- Conversation history is kept in gateway memory (last six exchanges) for the
  lifetime of the device session, and is discarded on disconnect.
- `realtime` listen mode has no barge-in: audio received while an answer is
  being produced is ignored.
- An utterance that yields no text ends with an empty response.
- A failed Mistral request closes the device session with `upstream_error`;
  `detail` names the stage and HTTP status, never the response body.
