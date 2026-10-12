# XiaoZhi Sovereign Gateway

A deliberately small gateway for stock XiaoZhi ESP32 firmware:

```text
XiaoZhi (WebSocket + Opus 16 kHz)
        ⇅
this gateway (state machine + libopus)
        ⇅
AI_BACKEND=openai:   OpenAI Realtime API (WebSocket + PCM16 24 kHz)
AI_BACKEND=mistral:  Mistral transcription + chat + speech (HTTPS)
AI_BACKEND=local:    speech-to-text + Ollama + text-to-speech on a local node
```

Version `v0.1-alpha` implements bootstrap, client/server hello, all three listen
modes, wake-word pre-roll, binary Opus, STT text, TTS start/sentence/stop,
abort, idle close, and clean device reconnects. See the
[frozen wire contract](docs/wire-protocol-v0.1.md).

## Privacy boundary

The gateway does not persist or log audio or transcripts and has no telemetry.
With `openai` or `mistral`, audio and text **leave the home network for
processing by that provider**. Review the provider's data terms before use, for
example OpenAI's [API data controls](https://platform.openai.com/docs/guides/your-data).
With `local`, speech recognition, the language model and speech synthesis run
on a separate compute node in your own network, and there is no fallback to a
cloud provider. The NAS itself runs only the lightweight gateway; local
inference is not a realistic workload for the DS218+.

Excluded by design: MQTT, MCP/tools, firmware OTA delivery, a web UI, analytics,
voice storage, transcript storage, and automatic Internet exposure.

## Runtime behavior

- Device input: one raw Opus packet per binary WebSocket frame, mono 16 kHz,
  60 ms (960 samples).
- OpenAI input/output: PCM16 mono 24 kHz over Realtime WebSocket JSON events.
- Device output: one raw Opus packet per frame, mono 24 kHz, 60 ms
  (1440 samples), paced in real time.
- `manual`: explicit `listen/stop` commits the input buffer.
- `auto` and `realtime`: the backend ends the turn; OpenAI with server VAD, the
  Mistral and local backends with silence detection in the gateway.
- Abort invalidates queued packets, cancels upstream work, and emits one
  `tts/stop` for that abort.
- Every reconnect receives a fresh gateway UUID and a fresh backend session.
  Conversation history does not survive reconnects.

## Backend boundary

The XiaoZhi layer (`protocol.py`) knows no provider. It drives a
`ConversationBackend` (`backend.py`) with 16 kHz PCM and turn commands, and
receives normalised events: user speech started/ended, user transcript,
response started/text/audio/done. Response audio is always 24 kHz PCM.
`openai_backend.py` holds resampling to OpenAI's 24 kHz input and all Realtime
event names. `mistral_backend.py` chains Mistral transcription, chat and
speech, and ends a turn with its own silence detection; see
[docs/mistral.md](docs/mistral.md).

## Security defaults

- Docker Hardened Images are mandatory build arguments and must be pinned by
  digest; an unpinned build fails.
- Runtime is non-root, read-only, capability-free, `no-new-privileges`, with
  memory and temporary-filesystem limits. DSM on the DS218+ does not expose the
  Docker cgroup controllers needed for hard CPU and PID limits; the Compose
  file therefore makes no claim that these are enforced.
- Host listeners bind only to `127.0.0.1`; DSM terminates trusted TLS.
- Provider and device credentials are mounted as files, never environment values.
- WebSocket protocol/version, device identity, token, state transitions, JSON,
  packet, turn, idle, and session limits are enforced.
- Logs contain a truncated SHA-256 device reference and technical events only.

See [SECURITY.md](SECURITY.md) before exposing anything beyond the LAN.

## Deploy on the DS218+

Follow [docs/synology-ds218plus.md](docs/synology-ds218plus.md). The short form:

1. Log the NAS Docker daemon into `dhi.io`, pull both named Python images, and
   place their immutable RepoDigests in `.env`.
2. Create `secrets/openai_api_key.txt` and a random
   `secrets/xiaozhi_device_token.txt`.
3. Build and start the Container Manager project with `compose.yaml`.
4. Route TLS port 443 to loopback port 18080 for OTA and TLS port 8443 to
   loopback port 18081 for WebSocket through DSM Reverse Proxy. This DSM
   version cannot route by URL path.
5. Point the board's Custom OTA URL at
   `https://<your host>.synology.me/xiaozhi/ota/`.

No router port-forward is needed for a board used only on the home LAN. If DSM
Firewall is enabled, allow TCP 8443 from the LAN only.

## Local tests

The code requires Python 3.12+, `libopus.so.0`, and `websockets==15.0.1`:

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The tests cover bootstrap output, strict hello validation, Opus encoding,
streaming resampling, wake-word coalescing, manual turn commit, abort semantics,
TTS/audio event ordering, the OpenAI event mapping, silence detection and the
Mistral pipeline. The Mistral tests run against a local stand-in for the
Mistral API (`tests/fake_mistral.py`). No test makes a paid provider request,
and none replaces the one-board network capture described in the NAS guide.

## Local backend

`AI_BACKEND=local` expects three HTTP services on a compute node in the LAN:

| Service | Request | Response |
|---|---|---|
| Speech-to-text (`LOCAL_STT_BASE_URL`) | `POST /transcribe`, body: PCM16 mono 16 kHz | JSON `{"text": "..."}` |
| Ollama (`OLLAMA_BASE_URL`) | `POST /api/chat`, streamed | Ollama NDJSON |
| Text-to-speech (`LOCAL_TTS_BASE_URL`) | `POST /speak`, JSON `{"text": "..."}` | WAV, mono, 16-bit |

The node may be switched off. The firmware first asks the gateway
`GET /compute/status` and, if needed, `POST /compute/wake`; the gateway then
asks a small helper on the NAS host (`host-helper/wol_helper.py`, reached
through the Unix socket mounted at `./run`) to send a Wake-on-LAN packet, waits
until the three services answer and loads the model. With `openai` and
`mistral` both endpoints answer `ready` at once.

These two endpoints are currently not authenticated. Keep the port that serves
them reachable from the LAN only.

The architecture behind this is described in [`../MyAI`](../MyAI/).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_WS_URL` | required | externally visible `wss://` endpoint |
| `DHI_PYTHON_BUILD_IMAGE` | required | digest-pinned DHI development image |
| `DHI_PYTHON_RUNTIME_IMAGE` | required | digest-pinned DHI runtime image |
| `AI_BACKEND` | `openai` | Conversation backend: `openai`, `mistral` or `local`; `BACKEND` is an older alias |
| `MISTRAL_MODEL` | `mistral-small-latest` | Mistral chat model |
| `MISTRAL_STT_MODEL` | `voxtral-mini-latest` | Mistral transcription model |
| `MISTRAL_TTS_MODEL` | `voxtral-mini-tts-2603` | Mistral speech model |
| `MISTRAL_VOICE_ID` | empty | Mistral voice; empty picks the first preset voice for the language |
| `MISTRAL_LANGUAGE` | `nl` | language hint for transcription and voice choice |
| `MISTRAL_MAX_SENTENCES` | `3` | Mistral backend: sentences per answer; `0` is no limit |
| `MISTRAL_TELL_DATE` | empty | Mistral backend: `1` gives the model the local date and time |
| `MISTRAL_TTS_GAIN` | `1.0` | Mistral backend: volume factor for the spoken answer |
| `VAD_THRESHOLD` | `300` | Mistral backend: minimum speech level (PCM16 RMS) |
| `VAD_SILENCE_MS` | `800` | Mistral backend: silence that ends a turn |
| `OPENAI_MODEL` | `gpt-realtime-2.1` | Realtime model |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | input transcript model |
| `OPENAI_VOICE` | `marin` | Realtime output voice |
| `TIMEZONE` | `Europe/Amsterdam` | time zone for the device clock, including daylight saving time |
| `TIMEZONE_OFFSET_MINUTES` | `0` | UTC offset used for the date given to the model (`MISTRAL_TELL_DATE`, `TELL_DATE`) |
| `OLLAMA_BASE_URL` | `http://192.168.1.24:11434` | local backend: Ollama on the compute node |
| `OLLAMA_MODEL` | `qwen2.5:7b` | local backend: model name |
| `OLLAMA_TIMEOUT_SECONDS` | `60` | local backend: Ollama request timeout |
| `LOCAL_STT_BASE_URL` | `http://192.168.1.24:11500` | local backend: speech-to-text service |
| `LOCAL_STT_TIMEOUT_SECONDS` | `30` | local backend: speech-to-text timeout |
| `LOCAL_TTS_BASE_URL` | `http://192.168.1.24:11501` | local backend: text-to-speech service |
| `LOCAL_TTS_TIMEOUT_SECONDS` | `30` | local backend: text-to-speech timeout |
| `COMPUTE_READY_TIMEOUT_SECONDS` | `90` | local backend: wait for the services after a wake |
| `XIAOZHI_ALLOWED_DEVICE_ID` | empty | optional exact device MAC allowlist |
| `MAX_TURN_SECONDS` | `60` | decoded input-audio limit |
| `MAX_SESSION_SECONDS` | `3300` | device session lifetime |
| `LOG_LEVEL` | `INFO` | technical log verbosity |

`INSTRUCTIONS` (or the older `OPENAI_INSTRUCTIONS`) may override the compact Dutch-safe system instruction.
An empty value in `.env` means "use the default".
Keep it in `.env` only if it contains no sensitive information.

## MVP limitations

- One shared device token and at most one configured device ID. A public
  multi-device release should use per-device enrollment, token hashes, and
  revocation.
- Linear 16→24 kHz resampling favors a tiny dependency surface over studio
  quality. Speech quality should be validated on the physical board.
- One `tts/sentence_start` is emitted per response, with the complete answer
  text. It is sent when the response is complete and after the user's `stt`
  text, so it can appear a moment after the audio starts.
- There is no fallback when the selected provider or the Internet is
  unavailable; the local backend deliberately never falls back to the cloud.
- The Mistral backend has no Dutch preset voice and no barge-in; see
  [docs/mistral.md](docs/mistral.md).
- The OpenAI, Mistral and local backends have been used on the x86-64 DS218+
  with the reference board; the Mistral backend in English only.
- The local backend, the compute endpoints and the Wake-on-LAN helper have no
  automated tests yet.

## License

Apache-2.0.
