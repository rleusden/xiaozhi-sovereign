# Firmware

## Scope

The firmware is a focused fork of the upstream [XiaoZhi ESP32 project](https://github.com/78/xiaozhi-esp32). The current development baseline is upstream tag `v2.5.0`, commit `ac6deed3d8e75348475364bf40ad953c6cd48054`. The current build targets the Freenove ESP32-S3 board with a 2.8-inch 240×320 display, 16 MB flash and 8 MB PSRAM used during development.

It intentionally retains the upstream audio, display and WebSocket foundations while changing the board experience and the service boundary.

## Sovereign build behaviour

The current build provides:

- a self-hosted OTA/bootstrap URL;
- Dutch language selection;
- wake-word detection disabled;
- physical start through the onboard BOOT button;
- an equivalent active-low external button on GPIO2 with internal pull-up;
- connection to GPIO2 by momentarily shorting it to GND;
- a bounded follow-up window after each answer;
- automatic WebSocket closure after inactivity;
- a local Delft-blue windmill idle screen;
- local date, time and battery indication;
- a compact conversation view showing user and assistant text;
- a three-line defensive display limit for assistant responses;
- return to the idle screen after prolonged UI inactivity;
- configuration access point prefix `XiaoZhi-Soeverein`.

## Interaction model

1. The device is idle and no conversation WebSocket is open.
2. The user presses BOOT or the GPIO2 button.
3. The device opens a session and enters listening state.
4. Questions and responses may continue during the bounded follow-up window.
5. After 20 seconds without a follow-up, the device closes the conversation channel.
6. If speech is active exactly at the deadline, one five-second grace period is allowed.
7. A new conversation requires another button press.

This model reduces unintended open-microphone time. It is not a hardware microphone disconnect: software and the ESP32 audio subsystem remain part of the trusted computing base.

## Differences from upstream firmware

| Concern | Upstream capability/default | Sovereign build decision |
|---|---|---|
| Service discovery | General XiaoZhi OTA/bootstrap configuration | Pinned to the owner-operated endpoint for this build |
| Wake word | Supported and commonly enabled | Disabled |
| Conversation activation | Board-dependent wake word/button/touch behaviour | BOOT or GPIO2 physical button |
| Follow-up session | Server/deployment-dependent | Locally bounded by firmware timer |
| User interface | General upstream themes and emoji assets | Dutch conversation view and local windmill idle screen |
| Text volume | General transcript display | Assistant display limited to three lines |
| Configuration AP | Upstream naming | `XiaoZhi-Soeverein-XXXX` |
| Chinese service dependency | Possible through default configuration | Removed from the selected build path |

The fork does not remove every upstream capability from the entire source tree. The security-relevant claim applies to the selected and reproducibly built board variant, not to arbitrary upstream configurations.

## Firmware trust and supply chain

A reproducible release should record:

- upstream commit and tag;
- fork commit;
- ESP-IDF version;
- selected board variant and build options;
- compiler/container image digest;
- firmware and merged-image SHA-256 hashes;
- partition table;
- SBOM or dependency inventory when available.

Do not publish only an unexplained binary. Publish source, build instructions, hashes and the exact board target together.

## Physical-security limitations

The development board exposes USB and programming interfaces. Without verified secure boot, flash encryption, protected debug access and a controlled key lifecycle, an attacker with the device may be able to read or replace firmware.

Enabling ESP32 security features affects recovery, OTA signing, manufacturing and the risk of permanently locking devices. It should be introduced through a documented provisioning design rather than enabled ad hoc.

## Testing expectations

Each release should verify at least:

- clean boot and correct board identity;
- bootstrap through the owner-controlled endpoint;
- TLS certificate validation;
- BOOT and GPIO2 activation;
- microphone capture and Opus uplink;
- response playback;
- first and subsequent follow-up questions;
- 20-second session closure and reconnect;
- Wi-Fi loss and recovery;
- date, time, battery and idle-screen behaviour;
- absence of configured upstream Chinese endpoints in the produced binaries.

Passing a build is not equivalent to passing a hardware test.

## Build

From `ESP32-Freenove-Firmware/`, using the documented ESP-IDF 6.1 container:

```sh
python3 scripts/build.py \
  freenove-esp32s3-display-2.8-lcd \
  --name xandria-freenove-esp32s3-display-2.8-lcd \
  --language nl-NL \
  --wake-word disabled \
  --zip
```

The selected board variant currently contains the operator's bootstrap URL. Change `CONFIG_OTA_URL` in the Freenove board `config.json` before building for another deployment. The provider key is never part of the firmware.
