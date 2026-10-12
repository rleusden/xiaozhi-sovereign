# MyAI — Local AI reference architecture

`MyAI` documents one working example of adding a local AI compute node to
xiaozhi-sovereign.

It is intended as **inspiration, not as a build recipe**.

The hardware used in this project was selected because it was inexpensive and
readily available on the local second-hand market. In another country or
situation, a mini-PC, NUC, workstation, server, laptop or other refurbished
x86 system may be a better choice.

The important part is therefore not the specific computer, but the
architecture and the design choices behind it.

## Goal

The local compute node provides:

- speech-to-text
- a local large language model
- text-to-speech

while remaining replaceable and separated from the ESP32 device.

The ESP32 does not know which AI technology is being used. It only communicates
with the sovereign gateway.

```text
ESP32
  |
  | XiaoZhi protocol
  v
Sovereign Gateway
  |
  +----------------------+----------------------+----------------------+
  |                      |                      |
  v                      v                      v
OpenAI                Mistral              Local AI
                                             |
                                             +-- STT
                                             +-- LLM
                                             +-- TTS

```

Reference implementation
The current reference setup uses a refurbished x86 desktop running Linux.
Its relevant characteristics are:
- x86_64 architecture
- sufficient RAM for a quantized local model
- wired Ethernet
- Wake-on-LAN support
- ability to run standard Linux services
- low acquisition cost on the local used market
- complete shutdown when local AI is not required
The exact make and model are not architectural requirements.

### Local AI stack

The current implementation uses:
- Ollama for the language model
- Qwen2.5 7B as the current model
- faster-whisper for speech-to-text
- Supertonic for text-to-speech

These components are also replaceable.
The gateway talks to services through explicit interfaces rather than depending
on the internal implementation of the compute node.

### Compute lifecycle

Local compute is treated as an on-demand resource.

```text
off
 |
 | Wake-on-LAN
 v
waking
 |
 | services become available
 v
warming
 |
 | model loaded
 v
ready
 |
 | conversation
 v
idle / warm
 |
 | timeout
 v
off
```

The gateway exposes this lifecycle to the ESP32 through a small HTTP interface:

GET  /compute/status
POST /compute/wake

For cloud backends, the gateway immediately reports ready.
For the local backend, it can wake the compute node and wait until the AI stack
is actually available.
This keeps backend-specific orchestration outside the ESP32 firmware.

## Design principles
The implementation follows a few simple principles:

- Replaceability

No individual AI provider, model or piece of hardware is a permanent dependency.

- Explicit trust boundary
  
The sovereign gateway is the boundary between the device and the selected AI
backend.
Local when useful, cloud when appropriate
Sovereignty does not require everything to run locally.
It requires knowing where data goes, being able to change that destination,
and retaining control over the interface.

- Power only when needed

A local AI server does not need to run 24/7.
The current design wakes the machine on demand and is being extended to keep
the model warm for a limited period before shutting the compute node down again.

## Why document this?
This directory is not intended to prescribe one correct local-AI setup.
Instead, it documents:
- what worked
- what did not
- architectural trade-offs
- lifecycle behavior
- security boundaries
- measured performance

The intention is to make it easier for others to design their own local AI
solution using hardware and software appropriate to their own environment.
