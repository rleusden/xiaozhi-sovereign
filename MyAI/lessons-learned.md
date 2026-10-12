# MyAI — Lessons Learned

## Purpose

This document captures practical lessons learned while building and evolving the MyAI reference implementation.

The objective is not to document every technical issue.

The useful lessons are the ones that changed the architecture, clarified a boundary or exposed an assumption that turned out to be wrong.

Many of them are small.

Together they explain why the current architecture looks the way it does.

---

## 1. Sovereignty is mostly about boundaries

At first, “sovereign AI” sounds like a hosting question:

> Should the model run locally or in the cloud?

In practice, that question is too narrow.

The more important questions are:

- Who decides where processing occurs?
- Can that decision be changed?
- Can a provider be removed?
- Are data flows visible?
- Are credentials controlled?
- Can the system operate without a specific external dependency?
- Can compute be stopped completely?

This led to an important distinction:

```text
Local AI != sovereign AI

Explicit control + replaceability + bounded dependencies
    = meaningful sovereignty
```

A cloud provider can still be part of a sovereign architecture if the dependency is explicit and replaceable.

A local model can still be a poor sovereignty choice if it creates hidden or irreversible dependencies.

---

## 2. The gateway became more important than the model

The original technical challenge appeared to be replacing a cloud AI backend.

The architectural breakthrough was realizing that the real asset is the gateway.

The gateway provides the stable boundary between:

```text
Client
  |
  v
Gateway
  |
  +-- OpenAI
  +-- Mistral
  +-- Local AI
```

Once that boundary existed, providers became implementation choices.

That changed the design question from:

> How do I connect this ESP32 to a local model?

to:

> How do I make the client independent of whichever backend is selected?

That is a much more reusable architecture.

---

## 3. Keep provider knowledge out of the client

It is tempting to solve local-AI behaviour directly in the ESP32 firmware.

For example:

```text
if local:
    wake computer
    wait for Ollama
```

That works quickly.

It is also the wrong abstraction.

The client should not know:

- whether a local LLM exists;
- whether Wake-on-LAN is used;
- whether Ollama is running;
- whether the provider is OpenAI or Mistral;
- or how long a model takes to load.

The better contract became:

> Is the selected backend ready?

This allowed the same firmware to work for cloud and local AI.

That removed an entire class of future client changes.

---

## 4. Generic interfaces age better than implementation-specific ones

The endpoints:

```http
GET /compute/status
POST /compute/wake
```

were initially created for local compute.

They became more useful once we generalized their semantics.

For cloud backends:

```json
{"state":"ready"}
```

can simply be returned immediately.

For local AI:

```text
off -> waking -> warming -> ready
```

may occur.

The lesson:

> A useful interface should describe intent, not expose internal machinery.

Even if an endpoint name originates from an implementation detail, its contract should remain provider-neutral where possible.

---

## 5. Local AI introduces infrastructure lifecycle

Cloud APIs hide most infrastructure lifecycle from the consumer.

Local AI does not.

Suddenly the architecture has to consider:

- power state;
- boot time;
- service startup;
- model load time;
- warm state;
- inactivity;
- shutdown;
- retries;
- race conditions.

The model is only one part of the system.

The local AI node behaves much more like infrastructure than like a library.

This was one of the clearest transitions from experiment to operational architecture.

---

## 6. “Running” is not the same as “ready”

A machine can respond to ping while the AI stack is unusable.

An LLM runtime can be running while the selected model is not loaded.

STT may be available while TTS has failed.

Therefore readiness must represent actual usable capability.

For the local reference stack:

```text
host reachable
    +
STT healthy
    +
LLM runtime healthy
    +
model available
    +
TTS healthy
    =
READY
```

This sounds obvious in hindsight.

It is easy to get wrong when first building a proof of concept.

---

## 7. Startup delay should be visible, not hidden

Cold startup takes time.

Trying to hide this behind a frozen interface creates a bad user experience.

The better approach was to make startup visible.

The ESP32 shows a sequence of Delft-blue wake-up screens while the gateway prepares the backend.

The important lesson is not the animation itself.

The lesson is:

> Long-running infrastructure transitions should have visible state.

The user should be able to distinguish:

```text
broken
```

from:

```text
working, but not ready yet
```

---

## 8. Cute UI can support serious architecture

The Delft-blue robot started as a friendly visual device.

It turned out to have architectural value.

Complex concepts such as:

- sovereignty;
- startup latency;
- physical control;
- provider boundaries;
- lifecycle state;

became easier to explain without turning the system into a wall of technical terminology.

The lesson was unexpected:

> Serious architecture does not require a severe visual language.

A friendly interface can make operational state more understandable without trivializing it.

---

## 9. LVGL cache behaviour can invalidate obvious-looking code

One of the wake-up animation bugs was especially instructive.

Logs showed:

```text
frame 1
frame 2
frame 3
...
frame 7
```

but the screen appeared to alternate between only a few images.

The application logic was correct.

The rendering layer was not behaving as expected because LVGL reused cached image descriptors.

The solution required explicitly dropping the previous image from the cache before replacing it.

The lesson is broader than LVGL:

> Correct control flow does not prove correct observable behaviour.

Instrumentation and visual verification both mattered.

---

## 10. TLS overhead matters on small devices

Repeated HTTPS status polling from an ESP32 exposed noticeable connection setup overhead.

Each fresh connection could cost several seconds.

A polling loop that looks harmless on a server can therefore behave very differently on an embedded client.

This led to a longer polling interval.

The lesson:

> Network behaviour must be evaluated from the weakest participant's perspective.

The ESP32 is not a laptop with unlimited CPU, memory and TLS acceleration.

---

## 11. Hardened containers sometimes need a narrow escape hatch

The gateway container was deliberately hardened:

- non-root;
- read-only filesystem;
- no capabilities;
- no-new-privileges.

That is desirable.

Wake-on-LAN, however, did not fit neatly inside those restrictions.

The temptation was to loosen the entire container.

Instead, a small host-side helper was introduced.

```text
Gateway container
      |
      v
narrow UNIX socket
      |
      v
WOL helper
```

The lesson:

> When one function needs privilege, isolate the privilege instead of expanding the trust boundary.

This pattern is reusable well beyond Wake-on-LAN.

---

## 12. No hidden fallback means accepting visible failure

Automatic cloud fallback is attractive:

```text
local failed -> use OpenAI
```

The user still receives an answer.

Availability appears excellent.

But the architecture has silently changed the processing location.

That conflicts with the purpose of selecting local processing.

The resulting rule became:

> If local is selected and local fails, fail visibly.

Changing backend may still be offered.

It should be an explicit action.

This is a useful example of a broader architectural trade-off:

```text
maximum convenience
        !=
maximum control
```

---

## 13. Local does not mean free

Using a local model removes per-request API billing.

It introduces other costs:

- hardware;
- electricity;
- memory;
- storage;
- cooling;
- maintenance;
- updates;
- startup time.

Keeping a desktop running all day simply to answer an occasional question would be a poor optimization.

That led to the on-demand lifecycle:

```text
off -> wake -> use -> warm idle -> shutdown
```

The lesson:

> Cost architecture should include physical resources, not only invoices.

---

## 14. Refurbished hardware can be an architecture choice

The local reference implementation uses a refurbished x86 desktop.

Not because that exact model is special.

It was simply:

- inexpensive;
- available locally;
- sufficiently powerful;
- expandable;
- compatible with standard Linux;
- capable of Wake-on-LAN.

The larger lesson is:

> Architecture should define required capabilities, not product names.

In another country or market, the correct implementation might be completely different.

The Fujitsu is an implementation detail.

The replaceable compute node is the architecture.

---

## 15. Measure before buying hardware

GPU options were considered early.

The CPU-only implementation was tested first.

That produced an actual baseline.

Only then does it make sense to ask whether a GPU would materially improve the user experience.

This avoids architecture driven by specification-sheet enthusiasm.

The rule became:

```text
measure
  ↓
identify bottleneck
  ↓
decide whether hardware changes matter
```

Not:

```text
buy hardware
  ↓
hope it solves something
```

---

## 16. Memory and lifecycle state should reflect reality

After a gateway restart, the lifecycle manager could report:

```text
state = off
```

while the local AI services were still running.

The services were real.

The state was merely an in-memory assumption.

This exposed an important lifecycle principle:

> Operational state should be reconstructed from observable reality where possible.

After restart, the gateway should probe:

- host reachability;
- STT;
- LLM;
- TTS;
- model readiness.

Then derive the current lifecycle state.

State should describe the system.

The system should not be assumed to match stale state.

---

## 17. Dependencies grow quietly

A particularly useful lesson came from outside the AI stack itself.

The Synology host was originally thought of primarily as:

- NAS;
- Docker host;
- gateway;
- reverse proxy.

During investigation it became clear that it was also the only DNS resolver distributed to clients.

Turning off the NAS could therefore make the entire network appear broken.

The actual dependency was:

```text
Client
  |
  v
Xandria DNS
  |
  v
Zyxel
```

The Synology DNS server existed primarily to provide one split-DNS record:

```text
xandria.synology.me -> local NAS address
```

A small convenience had quietly become a network-wide dependency.

The lesson is important:

> Architecture dependencies do not always arrive through architecture decisions.

Sometimes they grow from configuration.

Periodically asking:

> What stops working if I turn this machine off?

is therefore an excellent architecture test.

---

## 18. Redundancy does not automatically solve split DNS

Adding a second DNS server looked like an obvious availability improvement.

For ordinary internet DNS, it is.

For split DNS, the situation is more subtle.

If:

```text
DNS 1 -> normal public answer
DNS 2 -> local override
```

then clients may receive different answers depending on which resolver they use.

A “secondary DNS server” is not necessarily a passive standby.

The broader lesson:

> Redundancy must preserve semantic consistency, not just availability.

Two functioning components are not redundant if they disagree about the answer.

---

## 19. Reverse proxies create useful boundaries — and dependencies

The reverse proxy provides a clean TLS and WebSocket boundary in front of the gateway.

That is useful.

It also means internal hostname resolution, certificates and routing become part of the architecture.

Once multiple layers depend on a hostname, DNS is no longer an incidental detail.

The lesson:

> Every abstraction removes complexity from one place and introduces responsibility somewhere else.

That is not a reason to avoid abstraction.

It is a reason to document the resulting dependency.

---

## 20. Publishing architecture requires removing local accidents

The working environment contains many implementation details:

- IP addresses;
- hostnames;
- hardware addresses;
- local paths;
- SSH key names;
- network rules;
- device-specific decisions.

Those details are useful operationally.

They are poor reference architecture.

Preparing the repository for publication made the distinction clearer:

```text
Operational documentation:
    How this installation works

Reference architecture:
    Why the system is structured this way
```

Both are valuable.

They serve different audiences.

---

## 21. Repository hygiene is part of engineering

The development environment accumulated:

- backup files;
- generated files;
- old copies;
- logs;
- nested directories;
- temporary keys;
- CRLF contamination.

None of these are interesting architecture.

They can still create security, maintenance and review problems.

Cleaning the repository before publication reinforced another mundane but important lesson:

> A clean repository is evidence of understandable system boundaries.

Version control should contain intentional state.

Not historical debris.

---

## 22. Secrets deserve boring solutions

API keys and other credentials do not belong:

- in firmware;
- in Git;
- in command examples;
- or in architecture diagrams.

Keeping provider credentials at the gateway creates a useful concentration point for secrets.

This makes rotation, replacement and access control easier.

The lesson:

> Secret management should be boring, explicit and centralized.

Interesting secret management is usually a warning sign.

---

## 23. An embedded device should not become an infrastructure orchestrator

The ESP32 is capable of doing much more.

That does not mean it should.

Putting Wake-on-LAN, model knowledge, retry policies and service-health logic into the firmware would turn a small client into a distributed control plane.

That would be difficult to maintain.

The gateway is a better place for orchestration because it can evolve independently of the physical device.

The lesson:

> Capability is not responsibility.

---

## 24. A physical switch is still an architectural control

Modern systems often focus heavily on software controls:

- policies;
- identities;
- network filters;
- runtime permissions;
- application settings.

Those controls are essential.

They all depend on software continuing to behave as expected.

Physical power does not.

The ability to completely remove compute capability remains valuable.

Hence MayTag's contribution to the architecture:

> **Software controls are useful. Physical control is final.**

The lesson is not that software controls are weak.

The lesson is that layered controls are stronger than any single layer.

---

## 25. A prototype can reveal production questions surprisingly quickly

The project began as a small experiment with an ESP32 assistant.

It quickly raised questions about:

- provider independence;
- privacy;
- lifecycle;
- availability;
- observability;
- power management;
- privilege separation;
- DNS;
- failure behaviour;
- repository hygiene;
- evidence;
- governance.

That is typical of AI systems moving beyond demonstrations.

Getting a model response is often the easy part.

Operating the surrounding system responsibly is where architecture starts.

---

## What worked well

Several decisions proved particularly valuable:

- placing a gateway between client and provider;
- keeping provider secrets out of the ESP32;
- defining provider-neutral readiness;
- using local compute as an optional backend;
- keeping the local backend replaceable;
- refusing hidden cloud fallback;
- using observable lifecycle states;
- isolating privileged Wake-on-LAN functionality;
- testing CPU performance before buying a GPU;
- using visible startup feedback;
- treating local hardware as replaceable infrastructure.

These decisions reduced coupling rather than merely solving the immediate problem.

---

## What should be improved next

The current reference implementation still exposes useful areas for improvement.

### Reconstruct lifecycle state after restart

The gateway should derive state from actual health probes rather than default in-memory values.

### Complete idle shutdown

Implement the intended policy:

```text
response complete
      ↓
30 minute warm lease
      ↓
no new activity
      ↓
controlled shutdown
```

### Tighten DNS architecture

Ensure local host resolution does not make the gateway host an unintended network-wide single point of failure.

### Tighten recursive DNS access

Where a local DNS resolver remains necessary, recursion should be restricted to intended networks.

### Improve lifecycle evidence

Record timestamps for:

- wake request;
- host reachable;
- services ready;
- model warm;
- first conversation;
- idle;
- shutdown.

### Continue reducing implementation coupling

Provider-specific behaviour should continue moving behind stable interfaces.

---

## Final lesson

The project began with a simple question:

> Can this small AI device use my own backend instead of the original cloud?

The more useful question eventually became:

> Can every important dependency be seen, understood, replaced and stopped?

That changed the project from a device modification into an architecture exercise.

And that is probably the most important lesson learned.
