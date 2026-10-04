# v0.1-alpha

Release date: 2026-10-04

This is the first public technical preview of XiaoZhi Sovereign. It packages the two independently deployable components required by the reference device:

- `ESP32-Freenove-Firmware/`: full source for the modified Freenove ESP32-S3 build;
- `Docker-Gateway/`: the hardened XiaoZhi WebSocket/Opus to OpenAI Realtime gateway.

## Included behaviour

- owner-controlled HTTPS bootstrap and secure WebSocket endpoint;
- strict client/server hello and bounded protocol state machine;
- Opus audio, STT, TTS sentence lifecycle, abort and reconnect;
- BOOT or active-low GPIO2-to-GND conversation start;
- wake word disabled and no touch-to-start requirement;
- 20-second post-answer follow-up window, with one bounded speech grace period;
- Dutch conversation UI, three-line assistant display limit and local idle artwork;
- server time bootstrap, date, clock and battery status;
- digest-pinned Docker Hardened Images and non-root, read-only runtime;
- secret-file mounts and metadata-only logs.

## Validation status

The firmware/gateway baseline has completed real-board speech exchanges, repeated questions, response playback and timed WebSocket disconnects on the Freenove board and Synology DS218+ reference deployment. Gateway unit tests cover the bootstrap response, protocol validation, Opus, resampling, turn handling, abort and event ordering.

The final v8 source delta in this alpha fixes status-bar overlap and bounds the speech-at-deadline grace period. It has passed source review and gateway regression tests. A fresh ESP-IDF build and final on-device regression pass must still be recorded against the published commit before this alpha is promoted.

This release has not received an independent security audit. Secure Boot, flash encryption and a production provisioning/key lifecycle are not enabled by this source release.

## Known limitations

- The provider remains an external data processor; this is not an offline build.
- The gateway supports one shared device token and an optional single device-ID restriction.
- The Freenove build variant contains the reference operator's bootstrap hostname and must be changed for another deployment.
- There is no management UI, persistent conversation history, arbitrary tool execution or OpenClaw dependency.

## Licences

- Firmware fork: MIT; see `LICENSE` and the firmware's retained upstream notices.
- Gateway: Apache-2.0; see `GATEWAY_LICENSE` and `Docker-Gateway/LICENSE`.

