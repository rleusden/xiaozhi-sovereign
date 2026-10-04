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

