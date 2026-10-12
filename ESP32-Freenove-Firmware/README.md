# ESP32 Freenove firmware

This directory contains the complete XiaoZhi Sovereign firmware source for the Freenove ESP32-S3 2.8-inch display board. It is based on upstream XiaoZhi `v2.5.0`, commit `ac6deed3d8e75348475364bf40ad953c6cd48054`.

The sovereign board variant adds the Dutch UI, Delft-blue windmill idle screen, owner-controlled bootstrap endpoint, button-only conversation activation, GPIO2 active-low external button, bounded follow-up window and automatic session closure. Wake-word detection is disabled for this build.

Build with ESP-IDF 6.1:

```sh
python3 scripts/build.py \
  freenove-esp32s3-display-2.8-lcd \
  --name xandria-freenove-esp32s3-display-2.8-lcd \
  --language nl-NL \
  --wake-word disabled \
  --zip
```

Before building for another operator, replace `CONFIG_OTA_URL` in `main/boards/freenove-esp32s3-display-2.8-lcd/config.json`. Do not add provider credentials to the firmware.

See the repository-level [firmware documentation](../docs/FIRMWARE.md), [release notes](../RELEASE-v0.1-alpha.md) and retained [upstream README](UPSTREAM_README.md).


## Compute preflight and wake-up animation

This firmware adds a **compute preflight** before entering the normal XiaoZhi conversation flow.

Whether this check is needed, and where, comes from the gateway: its bootstrap (OTA) response contains a `compute` section, which the firmware stores at boot:

```json
"compute": {
  "required": true,
  "status_url": "https://<gateway host>/compute/status",
  "wake_url": "https://<gateway host>/compute/wake"
}
```

When `required` is true and the user presses the button, the device first checks whether the backend is ready:

1. `GET /compute/status`
2. if needed: `POST /compute/wake`
3. poll `/compute/status` until the state becomes `ready`
4. only then continue with the normal XiaoZhi WebSocket session

Each request carries the same credential as the WebSocket: `Authorization: Bearer <device token>` plus the `Device-Id` and `Client-Id` headers.

For a local backend this allows the gateway to wake and warm the local AI compute node before the conversation starts.

For cloud backends such as Mistral or OpenAI, `required` is false and the device connects directly, without the extra request. A gateway without a `compute` section is treated the same way. The firmware contains no gateway address of its own and does not need to know whether the AI runs locally or remotely.

The `compute` section is read at boot, so restart the device after changing the gateway's backend.

### Wake-up animation

While local compute is starting, the display shows a seven-frame Delft-blue wake-up animation.

![Wake-up animation](docs/wakeup-animation.gif)

The animation provides visible feedback during the cold-start period. As soon as the backend reports `ready`, the overlay is removed and the normal XiaoZhi UI takes over.

The final frame in the animated preview shows the familiar windmill idle screen.

### Current behavior

- Dutch (`nl-NL`) is the fixed firmware language.
- Compute readiness is checked over HTTPS before opening the XiaoZhi WebSocket.
- Local compute can transition through `off`, `waking`, `warming`, and `ready`.
- Cloud backends return `ready` immediately.
- The existing XiaoZhi WebSocket conversation flow remains unchanged once the preflight completes.
- The wake-up artwork is local to the firmware and does not depend on the AI provider.

### Design principle

The ESP does not know about Ollama, Wake-on-LAN, the Fujitsu compute host, Mistral, or OpenAI.

It only asks the sovereign gateway a simple question:

> Is the selected AI backend ready?

This keeps the responsibilities separated:

- **ESP firmware** handles the user interface and the normal XiaoZhi protocol.
- **Gateway** decides which AI backend is active and whether it is ready.
- **Local compute orchestration** remains an optional gateway-side capability.

This means the same firmware can be used with a fully local AI stack or with a remote provider, without changing the XiaoZhi conversation protocol.

