# MyAI — Security Architecture

## Purpose

This document describes the security principles and trust boundaries of the MyAI reference architecture.

It is not a complete hardening manual.

The objective is to make explicit:

- where trust changes;
- where secrets live;
- which components may communicate;
- which operations require privilege;
- how provider access is bounded;
- and how failures are contained.

The architecture assumes that AI components may change over time.

Security controls should therefore protect the boundaries between components rather than depend on one specific model, vendor or runtime.

---

## Security goals

The architecture aims to provide:

- explicit trust boundaries;
- minimal privilege;
- bounded network access;
- separation of provider credentials from clients;
- replaceable AI backends;
- no hidden cloud fallback;
- observable behaviour;
- controlled lifecycle operations;
- and the ability to remove compute capability completely.

The objective is not to create a perfectly isolated system.

The objective is to make dependencies and privileges visible and manageable.

---

## Trust model

A simplified trust model is:

```text
┌─────────────────────┐
│       ESP32         │
│   client device     │
└─────────┬───────────┘
          │
          │ controlled protocol
          ▼
┌─────────────────────┐
│ Sovereign Gateway   │
│                     │
│ policy              │
│ credentials         │
│ provider routing    │
│ lifecycle control   │
│ observability       │
└───────┬───────┬─────┘
        │       │
        │       │
        ▼       ▼
     Cloud     Local
     AI        AI
```

The gateway is the main trust boundary.

It decides:

- which backend is selected;
- where data is sent;
- which credentials are used;
- which operations are allowed;
- which infrastructure actions may occur.

The client is not trusted with provider credentials.

The AI provider is not trusted with gateway infrastructure privileges.

The local AI runtime is not trusted with unrestricted host access.

---

## Client trust boundary

The ESP32 is treated as an edge device.

Its responsibilities should remain limited.

It should not contain:

- cloud API keys;
- SSH credentials;
- infrastructure passwords;
- Wake-on-LAN implementation details;
- provider-specific secrets;
- or broad management capabilities.

This reduces the impact of:

- firmware disclosure;
- device loss;
- debugging access;
- accidental publication;
- or future firmware vulnerabilities.

The client should only receive the information required to perform its role.

---

## Gateway as policy boundary

The gateway is the point where security policy becomes enforceable.

Responsibilities include:

- backend selection;
- credential use;
- TLS termination;
- request routing;
- lifecycle decisions;
- logging;
- error handling;
- and access to privileged helper functions.

This centralizes sensitive functionality.

Conceptually:

```text
ESP32
  |
  v
Gateway
  |
  +-- policy
  +-- secrets
  +-- backend selection
  +-- lifecycle control
  +-- observability
```

The gateway should therefore be treated as trusted infrastructure.

---

## Secrets

Provider credentials should remain at the gateway.

Examples include:

- API keys;
- access tokens;
- backend credentials;
- service secrets.

They should not be:

- embedded in firmware;
- stored in source control;
- exposed through logs;
- copied into documentation;
- returned to clients.

A preferred pattern is:

```text
container
   |
   v
mounted secret file
```

rather than:

```text
source code -> embedded key
```

Secret management should remain simple and explicit.

> Secrets deserve boring solutions.

---

## Container hardening

The gateway reference implementation uses a hardened container model.

Relevant controls include:

- non-root execution;
- read-only root filesystem;
- no unnecessary Linux capabilities;
- `no-new-privileges`;
- explicit mounted secrets;
- limited exposed ports;
- controlled persistent storage.

The design principle is:

> The application container should receive only the privileges required for normal request processing.

If one exceptional capability requires more privilege, that capability should be isolated rather than weakening the entire container.

---

## Privilege separation

Wake-on-LAN is a useful example.

Sending WOL packets from the hardened gateway container proved awkward without relaxing container restrictions.

Instead of granting the complete container additional privileges, a small host-side helper performs the privileged operation.

Conceptually:

```text
Gateway container
       |
       | narrow local interface
       v
Host helper
       |
       | privileged network operation
       v
Compute node
```

This reduces the privileged attack surface.

The general rule is:

> Privilege should be granted to the smallest component that needs it.

---

## Host helper boundary

The host helper should expose only narrowly defined operations.

For example:

```text
wake compute node
```

is preferable to:

```text
execute arbitrary host command
```

The helper interface should therefore be:

- local where possible;
- minimal;
- authenticated or filesystem-permission protected;
- not reachable from untrusted networks;
- incapable of general command execution.

A convenience helper should not quietly become a remote administration interface.

---

## Network exposure

The preferred network flow is:

```text
ESP32
  |
  v
Gateway
  |
  v
Local AI services
```

The ESP32 should not require direct access to:

- Ollama;
- STT;
- TTS;
- SSH;
- host management interfaces.

The local compute node should accept AI service traffic only from the gateway where practical.

This minimizes exposed services and reduces coupling.

---

## Local AI firewall policy

A reasonable local-compute policy is:

```text
Gateway host  -> STT allowed
Gateway host  -> LLM allowed
Gateway host  -> TTS allowed

Other LAN clients -> denied
Internet          -> denied
```

The exact implementation depends on the operating system and network environment.

The architectural principle is stable:

> Local AI APIs should not automatically become LAN-wide APIs.

---

## Reverse proxy boundary

The gateway is exposed through a reverse proxy.

This provides a useful boundary for:

- TLS;
- WebSocket handling;
- certificate management;
- external versus internal exposure;
- service routing.

Conceptually:

```text
Client
  |
  | TLS
  v
Reverse proxy
  |
  v
Gateway
```

The reverse proxy should expose only required gateway endpoints.

Administrative interfaces should not automatically share the same exposure path.

---

## Internal addressing

Internal IP addresses, hostnames and service ports are implementation details.

They should not be treated as security controls by themselves.

An internal address is not equivalent to authentication.

Network location can reduce exposure, but sensitive interfaces should still be bounded through:

- firewall rules;
- protocol restrictions;
- authentication where appropriate;
- minimal functionality.

---

## DNS security considerations

The reference environment exposed an important dependency:

the gateway host also provided local DNS functionality.

This created two security and availability questions.

First:

```text
What happens if the gateway host is unavailable?
```

Second:

```text
Who is allowed to use the DNS resolver?
```

A recursive DNS resolver should not be broader than necessary.

Where local DNS remains in use, recursion should ideally be restricted to intended internal networks.

For example:

```text
allow recursion from trusted LAN
deny recursion from unintended networks
```

The exact configuration mechanism should preferably use the platform-supported administration interface rather than unmanaged edits that may be overwritten.

The broader lesson is:

> Security configuration should reflect actual trust zones, not defaults.

---

## Split DNS

Split DNS may be useful when the same hostname should resolve differently internally and externally.

For example:

```text
public DNS:
    service.example -> public address

internal DNS:
    service.example -> private address
```

This can simplify TLS and hostname consistency.

It also introduces an availability dependency.

If only one resolver knows the internal mapping, that resolver becomes part of the critical path.

Redundancy therefore has to preserve the same DNS semantics.

Two resolvers that return different intended answers are not true redundancy.

---

## SSH and administrative access

Administrative access to the local compute node should be separated from normal AI traffic.

A preferred future pattern is:

```text
Administrator
    |
    v
Gateway / bastion host
    |
    v
Local compute
```

Possible controls include:

- key-based authentication;
- no root login;
- limited users;
- source-IP restrictions;
- firewall rules;
- SSH service enabled only when needed.

The reference architecture does not require a specific bastion implementation.

The important principle is:

> Management access should not follow the same path as application traffic.

---

## AI provider boundary

Cloud AI providers should be treated as external processing dependencies.

The gateway should make backend selection explicit.

For each provider, architecture should be able to answer:

- What data is sent?
- When is it sent?
- Which credentials are used?
- Which provider processes it?
- Can the provider be removed?
- What happens if it is unavailable?

A provider should not receive data merely because another backend failed.

---

## No hidden cloud fallback

One of the strongest privacy and sovereignty controls is intentionally simple:

> If local processing is selected, local failure must not silently cause cloud processing.

Incorrect:

```text
local selected
      |
      v
local unavailable
      |
      v
automatic cloud processing
```

Preferred:

```text
local selected
      |
      v
local unavailable
      |
      v
visible failure
      |
      +--> retry local
      +--> explicitly select another backend
```

This makes processing location a policy decision instead of an availability side effect.

---

## Logging

Operational logging is necessary for:

- troubleshooting;
- evidence;
- performance analysis;
- provider verification;
- lifecycle analysis.

Logging should not automatically mean storing conversation content.

A useful distinction is:

```text
event logging
    !=
conversation recording
```

Examples of low-content operational events include:

```text
device_connected
backend_selected
listen_start
speech_end
compute_state
turn_timing
response_complete
```

Where possible, logs should capture behaviour rather than unnecessary user content.

---

## Sensitive logging

Logs may still contain sensitive information indirectly.

Potential risks include:

- prompts;
- transcriptions;
- returned model text;
- API errors containing request data;
- tokens;
- URLs with credentials;
- stack traces exposing local paths.

Logging should therefore follow the same minimization principle as other data flows:

> Log what is required to operate and demonstrate the system, not everything that is available.

Debug logging deserves particular care.

---

## Observability as a security control

Observability is not only an operational concern.

It can provide evidence that:

- the expected backend was used;
- no unexpected fallback occurred;
- lifecycle transitions happened as designed;
- privileged actions occurred;
- startup failed before processing;
- traffic remained within intended boundaries.

This supports demonstrable control.

A system that claims local processing but cannot show which backend handled a request provides weaker assurance than one that records the relevant decision.

---

## AI agents and MCP

Adding agents or MCP-style tool access changes the threat model significantly.

A conversational AI that can only generate text has limited direct capability.

A model that can invoke tools may be able to:

- read files;
- send messages;
- modify systems;
- call APIs;
- operate infrastructure.

Tool access must therefore be explicitly bounded.

Conceptually:

```text
Model
  |
  v
Policy boundary
  |
  +--> allowed tool
  +--> allowed read-only resource
  +--> denied action
```

The model should not automatically inherit the permissions of the gateway host.

---

## Tool permissions

Future tool integrations should follow principles such as:

- least privilege;
- explicit allow-lists;
- bounded scopes;
- read-only access where possible;
- human confirmation for high-impact actions;
- auditable calls;
- revocable credentials.

Tool permissions should describe intended capability rather than provide broad shell-like access.

---

## Model output is untrusted input

LLM output should not automatically be treated as authoritative instructions.

This becomes especially important when output influences:

- tools;
- infrastructure;
- files;
- network requests;
- lifecycle operations.

The architecture should maintain a distinction between:

```text
model proposes action
```

and:

```text
system authorizes action
```

The policy boundary must remain outside the model.

---

## Lifecycle security

Wake and shutdown operations affect availability and resource use.

They are therefore security-relevant operations.

A lifecycle API should not permit arbitrary external clients to:

- repeatedly wake hardware;
- force shutdowns;
- consume compute indefinitely;
- create denial-of-service conditions.

Lifecycle actions should be restricted to trusted components.

The ESP32 may request readiness.

The gateway should remain responsible for deciding how that request translates into infrastructure action.

---

## Denial-of-service considerations

Local AI compute has finite resources.

Potential abuse includes:

- repeated wake requests;
- long-running model prompts;
- excessive audio streams;
- resource exhaustion;
- intentionally preventing idle shutdown.

Useful future controls may include:

- rate limits;
- maximum request duration;
- buffer limits;
- concurrent-session limits;
- lifecycle lease limits;
- authentication.

The exact controls depend on the intended exposure.

---

## Buffer and stream limits

Audio and streaming interfaces should be bounded.

Unlimited buffers can become both reliability and security risks.

Relevant controls include:

- maximum frame size;
- maximum queued audio;
- request timeout;
- connection timeout;
- cleanup after disconnect;
- maximum response length where appropriate.

These are ordinary defensive engineering controls.

AI does not remove the need for them.

---

## Dependency integrity

Third-party components include:

- firmware libraries;
- container images;
- AI runtimes;
- speech models;
- language models;
- host packages.

The architecture should assume that dependencies will require:

- updates;
- replacement;
- vulnerability review;
- lifecycle management.

Replaceability is therefore also a security property.

A component that cannot realistically be removed becomes a long-term risk.

---

## Updates

Updates should preserve the same trust boundaries.

For example, adding a new backend should not require:

- embedding new credentials in the ESP32;
- exposing new local ports to the entire LAN;
- granting the gateway container broad new privileges.

If an update requires weakening a boundary, that should be treated as an architectural decision rather than a routine implementation detail.

---

## Failure containment

Components should fail within their intended boundaries.

Examples:

```text
Cloud provider failure
    -> cloud backend unavailable
    -> local network remains unaffected
```

```text
Local AI failure
    -> local backend unavailable
    -> gateway remains operational
```

```text
ESP32 failure
    -> one client unavailable
    -> backend infrastructure unaffected
```

```text
Model failure
    -> no automatic infrastructure privilege escalation
```

Good boundaries prevent one failure from becoming system-wide failure.

---

## Gateway criticality

The gateway intentionally centralizes several functions.

This provides control.

It also makes the gateway important.

Its failure may affect:

- provider access;
- lifecycle control;
- device connectivity;
- reverse proxy routing.

This should be acknowledged rather than hidden.

The architecture should periodically ask:

> Has a useful control point become an unnecessary single point of failure?

Not every single point needs immediate redundancy.

It should, however, be intentional.

---

## Physical security boundary

The local compute node has one control that cloud systems do not provide directly:

it can be physically powered off.

This is useful as a final control when:

- software state is uncertain;
- maintenance is required;
- the system should be intentionally unavailable;
- experimentation must stop.

Normal operations should use controlled software lifecycle management.

Physical power is the last layer.

> **Software controls are useful. Physical control is final.**  
> — MayTag

---

## Security by explicit architecture

The architecture does not assume that any particular provider or model is malicious.

The motivation is simpler.

If the properties of an external system cannot be independently established to the required level, the architecture should not depend blindly on them.

A sovereign boundary provides control without requiring accusations about external parties.

The relevant question is:

> Can this system demonstrate where data goes, what processes it and who controls the dependency?

---

## Security principles

The security architecture can be summarized as follows.

### 1. Keep secrets out of clients

Provider credentials belong at the gateway.

### 2. Minimize privilege

Do not weaken an entire container for one privileged operation.

### 3. Limit network exposure

Only required systems should reach AI services.

### 4. Make provider choice explicit

Processing location should never change silently.

### 5. Treat model output as untrusted

AI may propose actions; policy authorizes them.

### 6. Separate application and administration paths

User traffic and infrastructure management are different trust domains.

### 7. Log behaviour, not everything

Observability should support evidence without becoming unnecessary surveillance.

### 8. Bound tools and agents

Tool access must be scoped, auditable and revocable.

### 9. Design for failure containment

A component failure should remain within its boundary where possible.

### 10. Preserve physical control

Software policy is useful; power removal remains final.

---

## Security backlog

The current reference implementation suggests several useful next steps.

### Restrict recursive DNS

If local DNS remains necessary, limit recursion to trusted internal networks.

### Tighten SSH access

Prefer:

```text
key authentication
no root login
restricted source systems
minimal enabled users
```

### Add lifecycle authorization

Ensure readiness and wake interfaces cannot become uncontrolled infrastructure operations.

### Add stream limits

Bound audio buffering, request duration and connection behaviour.

### Improve startup-state reconstruction

Do not trust in-memory lifecycle state after gateway restart.

### Review container images and dependencies

Keep runtime components replaceable and updateable.

### Formalize agent/tool permissions

Before adding MCP or agent actions, define explicit capability boundaries.

### Review DNS dependency

Ensure split DNS does not unintentionally make the gateway host critical to unrelated network availability.

---

## Final perspective

Security in this architecture is not primarily a collection of individual controls.

It is the consequence of keeping boundaries explicit.

The ESP32 should not become an infrastructure controller.

The model should not become a policy engine.

The gateway should not receive unnecessary privilege.

The local AI node should not become an unrestricted LAN service.

Cloud fallback should not override processing choices.

And operational convenience should not quietly create hidden dependencies.

The goal is simple:

> **Every important capability should have a clear owner, a clear boundary and a clear way to stop it.**

That is the security model behind MyAI.
