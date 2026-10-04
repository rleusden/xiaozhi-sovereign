# XiaoZhi Sovereign Gateway

A deliberately small gateway for stock XiaoZhi ESP32 firmware:

```text
XiaoZhi (WebSocket + Opus 16 kHz)
        ⇅
this gateway (state machine + libopus)
        ⇅
AI_BACKEND=openai:   OpenAI Realtime API (WebSocket + PCM16 24 kHz)
AI_BACKEND=mistral:  Mistral transcription + chat + speech (HTTPS)
```

Version `v0.1-alpha` implements bootstrap, client/server hello, all three listen
modes, wake-word pre-roll, binary Opus, STT text, TTS start/sentence/stop,
abort, idle close, and clean device reconnects. See the
[frozen wire contract](docs/wire-protocol-v0.1.md).

## Privacy boundary

The gateway does not persist or log audio or transcripts and has no telemetry.
Audio and text **do leave the home network for processing by the selected
provider**, OpenAI or Mistral AI. This is a self-hosted alternative to the
stock service, not an offline system. Review the provider's data terms before
use, for example OpenAI's [API data controls](https://platform.openai.com/docs/guides/your-data). Local STT/LLM/TTS inference is not part of this project and is not a
realistic workload for the DS218+; the NAS runs only the lightweight gateway.

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
  Mistral backend with silence detection in the gateway.
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

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_WS_URL` | required | externally visible `wss://` endpoint |
| `DHI_PYTHON_BUILD_IMAGE` | required | digest-pinned DHI development image |
| `DHI_PYTHON_RUNTIME_IMAGE` | required | digest-pinned DHI runtime image |
| `AI_BACKEND` | `openai` | Conversation backend: `openai` or `mistral` |
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
| `TIMEZONE_OFFSET_MINUTES` | `0` | device display offset from UTC, in minutes |
| `XIAOZHI_ALLOWED_DEVICE_ID` | empty | optional exact device MAC allowlist |
| `MAX_TURN_SECONDS` | `60` | decoded input-audio limit |
| `MAX_SESSION_SECONDS` | `3300` | device session lifetime |
| `LOG_LEVEL` | `INFO` | technical log verbosity |

`INSTRUCTIONS` (or the older `OPENAI_INSTRUCTIONS`) may override the compact Dutch-safe system instruction.
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
- There is no local fallback when the provider or the Internet is unavailable.
- The Mistral backend has no Dutch preset voice and no barge-in; see
  [docs/mistral.md](docs/mistral.md).
- Both backends have been exercised on the x86-64 DS218+ with the reference
  board. The Mistral backend was exercised in English only.

## License

Apache-2.0.

## Provider selection and log privacy

`AI_BACKEND` is the preferred setting. Existing deployments using `BACKEND`
continue to work. If both are set, they must select the same provider; invalid
values or conflicting selections fail startup before any secret is read. If
neither is set, OpenAI remains the default. The selected provider is included
in the metadata-only `gateway_ready` event.

Application `LOG_LEVEL=DEBUG` does not enable WebSocket wire diagnostics:
those can expose headers and message content and are suppressed. Secret fields
are excluded from the configuration representation. This does not change the
requirement to keep content and credentials out of application log fields.

Compose still mounts both provider secret files in this release; Python loads
only the selected provider key. Provider-specific mounts, stream completion
validation, buffer limits and bounded cleanup remain follow-up work.
