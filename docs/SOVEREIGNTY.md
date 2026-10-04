# Sovereignty principles

## What sovereignty means here

For this project, digital sovereignty means the practical ability to understand, operate, replace and stop every material part of the voice-assistant data path.

It is not synonymous with offline operation, European hosting or open source. Those properties can strengthen sovereignty, but none is sufficient on its own.

The project uses five working principles:

1. **Owner-controlled entry point** — the device contacts infrastructure selected by its owner.
2. **Explicit processing chain** — every system that receives audio, text, credentials or metadata is documented.
3. **Replaceable external services** — provider-specific behaviour belongs behind a gateway boundary.
4. **Local authority** — a physical control starts the session, and local firmware can end it without provider cooperation.
5. **Honest limitations** — current external processors, residual risks and missing controls are stated plainly.

## Trust boundaries

| Boundary | Data crossing it | Required controls |
|---|---|---|
| User → device | Voice and button input | Visible listening state, explicit initiation, bounded session |
| Device → reverse proxy | Device metadata, protocol messages and Opus audio | TLS, device authentication, restricted endpoint |
| Reverse proxy → gateway | Decrypted device traffic | Local network restriction, minimal routing, patched platform |
| Gateway → AI provider | Audio, prompts, transcripts and response audio as required | Provider credentials, contractual/privacy assessment, retention configuration |
| Administrator → platform | Configuration, secrets and logs | Least privilege, access control, backups and secret rotation |

## Data-flow statement

During an active session, microphone audio is encoded on the ESP32 and sent over a secure WebSocket to the owner-controlled endpoint. The gateway decodes or resamples audio when required and forwards the conversation to the configured AI backend. Response audio and protocol state return through the same gateway.

The backend is OpenAI Realtime or Mistral AI, as configured by the owner. In both cases the required conversation data leaves the local environment and is processed by that provider: OpenAI in the first case, Mistral AI, a company established in France, in the second. Choosing Mistral changes which legal entity and jurisdiction process the data; it does not make the configuration offline or local, and the project does not describe either configuration that way.

Outside an active session, the intended design is that no microphone audio is sent to the gateway. A bounded follow-up window closes the WebSocket session after the answer unless the user begins a follow-up interaction.

## Data minimisation

The gateway should process only what is required for the current conversation. With the Mistral backend the gateway itself keeps the last exchanges of the conversation in memory, because the provider holds no session; that history is discarded when the device disconnects.

The following apply to both backends:

- no raw-audio archive;
- no transcript logging;
- no prompt or response logging;
- no API keys or device tokens in logs;
- no conversation database in the minimal gateway;
- no analytics or telemetry by default;
- short, bounded sessions rather than permanent microphone connections.

Operational logs should contain only events such as connection, listening state, response completion, sanitized error category and disconnection.

## Control and replaceability

The gateway is the architectural seam between device protocol and AI backend. A future provider adapter may target a European provider or a model operated locally. That change must not require provider credentials in the firmware or a redesign of the device protocol.

Replaceability is a design requirement, not evidence that every adapter already exists. At present the OpenAI Realtime and Mistral paths are implemented; switching between them is one gateway setting and needs no firmware change. A local-model backend does not exist yet.

## Deployment choices

- **Home/NAS gateway + cloud model:** local control over device access and routing; external AI processing remains.
- **EU-hosted gateway + cloud model:** operational location changes, but provider processing and legal roles still require assessment.
- **Local gateway + local model:** strongest locality, but requires sufficient compute, model governance, patching and operational capability.
- **Third-party managed gateway:** may reduce operational burden while transferring control and trust to another party.

No topology is automatically compliant. The controller must document purpose, lawful basis, processors, retention, access and user information for the actual deployment.

## Security is part of sovereignty

Control without adequate security is fragile. The reference deployment therefore uses:

- TLS at the public endpoint;
- a device token and optional allow-listed device identifier;
- secrets mounted as files instead of compiled into firmware;
- an unprivileged container user;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges`;
- bounded memory and temporary storage;
- minimal dependencies and protocol surface.

These controls reduce risk but do not replace patch management, monitoring, backups, network segmentation or incident response.

## Explicit non-claims

This project does not claim:

- that no data ever leaves the local network;
- that using a European server alone creates sovereignty;
- that open source code is automatically secure;
- that the firmware resists a capable attacker with physical access;
- that the system is GDPR-compliant without deployment-specific governance;
- that a local-model backend already exists;
- that the Mistral backend can speak every language it can transcribe: it has no Dutch preset voice.

The goal is not technological purity. It is informed, reversible control over a clearly documented system.
