# XiaoZhi OpenAI Gateway MVP

A deliberately small gateway for stock XiaoZhi ESP32 firmware:

```text
XiaoZhi (WebSocket + Opus 16 kHz)
        ⇅
this gateway (state machine + libopus + 16→24 kHz resampling)
        ⇅
OpenAI Realtime API (WebSocket + PCM16 24 kHz)
```

Version `v0.1-alpha` implements bootstrap, client/server hello, all three listen
modes, wake-word pre-roll, binary Opus, STT text, TTS start/sentence/stop,
abort, idle close, and clean device reconnects. See the
[frozen wire contract](docs/wire-protocol-v0.1.md).

## Privacy boundary

The gateway does not persist or log audio or transcripts and has no telemetry.
Audio and text **do leave the home network for OpenAI processing**. This is an
OpenAI-backed alternative to the stock service, not an offline system. Review
OpenAI's current [API data controls](https://platform.openai.com/docs/guides/your-data)
before use. Local STT/LLM/TTS inference is not part of this project and is not a
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
- `auto` and `realtime`: OpenAI server VAD commits and creates a response.
- Abort invalidates queued packets, cancels upstream work, and emits one
  `tts/stop` for that abort.
- Every reconnect receives a fresh gateway UUID and a fresh OpenAI session.
  Conversation history does not survive reconnects.

## Security defaults

- Docker Hardened Images are mandatory build arguments and must be pinned by
  digest; an unpinned build fails.
- Runtime is non-root, read-only, capability-free, `no-new-privileges`, with
  memory and temporary-filesystem limits. DSM on the DS218+ does not expose the
  Docker cgroup controllers needed for hard CPU and PID limits; the Compose
  file therefore makes no claim that these are enforced.
- Host listeners bind only to `127.0.0.1`; DSM terminates trusted TLS.
- OpenAI and device credentials are mounted as files, never environment values.
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
   `https://xandria.synology.me/xiaozhi/ota/`.

No Zyxel port-forward is needed for a board used only on the home LAN. If DSM
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
and TTS/audio event ordering. They do not make a paid OpenAI request and do not
replace the one-board network capture described in the NAS guide.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_WS_URL` | required | externally visible `wss://` endpoint |
| `DHI_PYTHON_BUILD_IMAGE` | required | digest-pinned DHI development image |
| `DHI_PYTHON_RUNTIME_IMAGE` | required | digest-pinned DHI runtime image |
| `OPENAI_MODEL` | `gpt-realtime-2.1` | Realtime model |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | input transcript model |
| `OPENAI_VOICE` | `marin` | Realtime output voice |
| `TIMEZONE_OFFSET_MINUTES` | `0` | device display offset from UTC, in minutes |
| `XIAOZHI_ALLOWED_DEVICE_ID` | empty | optional exact device MAC allowlist |
| `MAX_TURN_SECONDS` | `60` | decoded input-audio limit |
| `MAX_SESSION_SECONDS` | `3300` | device session lifetime |
| `LOG_LEVEL` | `INFO` | technical log verbosity |

`OPENAI_INSTRUCTIONS` may override the compact Dutch-safe system instruction.
Keep it in `.env` only if it contains no sensitive information.

## MVP limitations

- One shared device token and at most one configured device ID. A public
  multi-device release should use per-device enrollment, token hashes, and
  revocation.
- Linear 16→24 kHz resampling favors a tiny dependency surface over studio
  quality. Speech quality should be validated on the physical board.
- One `tts/sentence_start` is emitted per response. The display gets the text
  accumulated when the first audio packet is due, not guaranteed full text.
- There is no local fallback when OpenAI or the Internet is unavailable.
- The image build and live Realtime exchange must still be validated on the
  x86-64 DS218+ with real DHI credentials and an OpenAI API key.

## License

Apache-2.0.
