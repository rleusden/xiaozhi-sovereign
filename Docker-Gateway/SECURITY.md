# Security policy

This gateway intentionally has no telemetry, transcript storage, audio storage,
MCP, OTA firmware delivery, or web management interface. Logs contain only
technical event names, modes, error classes, and a truncated one-way device
reference.

## Secret handling

- Put secrets only in `secrets/*.txt`; that directory is git-ignored.
- Never place an OpenAI key in `.env`, Compose YAML, an image layer, or logs.
- On the DS218+, make secret files owned by numeric UID/GID `65532:65532` with
  mode `0400`; Compose preserves host file ownership for these mounts.
- Rotate both secrets after accidental disclosure.
- Keep host ports bound to `127.0.0.1`; expose only DSM's TLS reverse proxy.
- Set `XIAOZHI_ALLOWED_DEVICE_ID` after the first successful connection.

## Reporting

Do not publish an active credential, audio, transcript, packet capture, MAC
address, or client UUID in an issue. Provide a minimal redacted reproduction.
