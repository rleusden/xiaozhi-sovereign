# DS218+ / DSM 7.4 deployment

The container is intentionally reachable only through DSM's TLS reverse proxy.
Do not forward ports 18080 or 18081 on the Zyxel router.

## 1. Pin the Docker Hardened Images

Enable SSH temporarily in DSM and log in to the NAS. Authenticate with a Docker
ID that can pull Docker Hardened Images:

```sh
sudo docker login dhi.io
sudo docker pull dhi.io/python:3.13-alpine3.23-dev
sudo docker pull dhi.io/python:3.13-alpine3.23
```

Now request and label each digest separately. Docker omits the original tag in
this output, so do not paste two unlabeled lines into `.env`:

```sh
echo 'BUILD IMAGE (-dev):'
sudo docker image inspect --format '{{index .RepoDigests 0}}' \
  dhi.io/python:3.13-alpine3.23-dev

echo 'RUNTIME IMAGE:'
sudo docker image inspect --format '{{index .RepoDigests 0}}' \
  dhi.io/python:3.13-alpine3.23
```

For this DS218+ the results on 25 September 2026 were:

```text
BUILD:   dhi.io/python@sha256:96af06a88f92b774394c25aa0b7f758b25495af7e53f28e0e433f35bafd6f5da
RUNTIME: dhi.io/python@sha256:193755a5668ba0a0cf75e3c295ac706c0b0d50e2d585e9a0fa9a65da406e65ee
```

The supplied `example.env` already maps those results to the correct full
references:

```dotenv
DHI_PYTHON_BUILD_IMAGE=dhi.io/python:3.13-alpine3.23-dev@sha256:96af06a88f92b774394c25aa0b7f758b25495af7e53f28e0e433f35bafd6f5da
DHI_PYTHON_RUNTIME_IMAGE=dhi.io/python:3.13-alpine3.23@sha256:193755a5668ba0a0cf75e3c295ac706c0b0d50e2d585e9a0fa9a65da406e65ee
```

The tag makes the role readable; the digest makes the selected content
immutable. The project refuses to build if either variable is missing.

## 2. Files and secrets

Extract the project into `/volume1/docker/xiaozhi-gateway` and run:

```sh
cd /volume1/docker/xiaozhi-gateway
cp example.env .env
mkdir -m 700 secrets
umask 077
printf '%s' 'sk-proj-REPLACE' > secrets/openai_api_key.txt
openssl rand -hex 32 > secrets/xiaozhi_device_token.txt
chmod 600 .env
sudo chown 65532:65532 secrets/*.txt
sudo chmod 0400 secrets/*.txt
```

Compose mounts these files without changing their host ownership. The container
runs as numeric UID/GID `65532:65532`, so `0600` files owned by the NAS login
user cause `PermissionError` inside the container. Numeric ownership plus
read-only owner mode lets only root on the NAS and the non-root container user
read them. Do not work around this with `chmod 0644`.

Verify without displaying either secret:

```sh
sudo stat -c '%u:%g %a %n' secrets/*.txt
```

Both lines must start with `65532:65532 400`.

`PUBLIC_WS_URL` is already set to
`wss://xandria.synology.me:8443/xiaozhi/ws` in the supplied file. Leave
`XIAOZHI_ALLOWED_DEVICE_ID` empty for the first connection. Afterwards put the
exact MAC from the device's `Device-Id` header there and recreate the container.

## 3. Build and start

Create a Container Manager Project from the folder and select `compose.yaml`, or
use SSH:

```sh
sudo docker compose config
sudo docker compose build --pull
sudo docker compose up -d
sudo docker compose ps
curl http://127.0.0.1:18080/healthz
```

`docker compose config` must show a digest on both base-image build arguments.
It also expands secrets in the environment, so do not paste its output online.

The DS218+ DSM kernel does not expose the cgroup controllers used by Docker's
`NanoCPUs` and PID-limit settings. For that reason this Compose file
intentionally has neither a `cpus:` nor a `pids_limit:` key. Adding `cpus:`
prevents container creation with:

```text
NanoCPUs can not be set, as your kernel does not support CPU CFS scheduler
```

Adding `pids_limit:` produces a warning and Docker silently discards the limit.
Leaving either setting in the public configuration would therefore create false
assurance. The gateway is event-driven and creates only a small fixed number of
tasks, but that is application behavior rather than kernel enforcement.

The 384 MiB memory limit, read-only filesystem, dropped capabilities, and
`no-new-privileges` remain configured. Verify their effective runtime values
after creation rather than trusting the Compose source alone:

```sh
sudo docker inspect xiaozhi-openai-gateway --format \
  'memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}} readonly={{.HostConfig.ReadonlyRootfs}} capdrop={{json .HostConfig.CapDrop}} securityopt={{json .HostConfig.SecurityOpt}} user={{.Config.User}}'
```

Expected: `memory=402653184`, `pids=0`, `readonly=true`, `capdrop=["ALL"]`,
`securityopt=["no-new-privileges:true"]`, and `user=65532:65532`. Here `pids=0`
means unsupported/unlimited and is explicitly documented, not treated as a
successful hardening control.

The first `# syntax=...` line belongs in `Dockerfile`; entering it at the shell
prompt only creates a shell comment and changes nothing. Verify the file with:

```sh
head -n 1 Dockerfile
```

## 4. DSM reverse proxy

DSM 7.4.1 on this DS218+ has no source-path or destination-path fields. It can
therefore route only on hostname and port. Using two rules on the same
`xandria.synology.me:443` tuple would be ambiguous. Keep one hostname and split
the services across two TLS ports instead.

In **Control Panel → Login Portal → Advanced → Reverse Proxy**, create these two
HTTPS rules using the existing wildcard certificate for
`*.xandria.synology.me`:

| Source | Destination |
|---|---|
| `https://xandria.synology.me:443` | `http://127.0.0.1:18080` |
| `https://xandria.synology.me:8443` | `http://127.0.0.1:18081` |

Enable WebSocket support on the second rule. DSM normally adds the required
`Upgrade` and `Connection` headers when that option is enabled. Do not forward
either port on the Zyxel. If DSM Firewall is enabled, allow TCP 8443 only from
the LAN.

Validate from a LAN computer:

```sh
curl -i https://xandria.synology.me/xiaozhi/ota/
```

The response must contain only a `websocket` object whose URL starts with
`wss://xandria.synology.me:8443/`. Never publish that response: it contains the
device credential.

Validate the WebSocket proxy without using a real token:

```sh
curl --http1.1 \
  --resolve xandria.synology.me:8443:192.168.1.102 \
  --max-time 3 \
  -sS -D - -o /dev/null \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://xandria.synology.me:8443/xiaozhi/ws
```

`101 Switching Protocols` proves TLS, DSM proxying, and WebSocket upgrade. A
subsequent curl timeout is expected: curl doesn't complete the XiaoZhi
application handshake and doesn't treat the received WebSocket close frame as
a normal HTTP response ending.

## 5. Board migration and capture

Set the board's custom OTA URL to
`https://xandria.synology.me/xiaozhi/ota/`. Keep `xandria.synology.me` resolving
to `192.168.1.102` on LAN DNS. Do not add an Internet port-forward unless remote
device use is an explicit requirement.

For the first board only, capture technical metadata long enough to confirm:

- client hello says Opus, 16 kHz, mono, 60 ms;
- manual mode sends `listen/start`, binary packets, then `listen/stop`;
- WebSocket control ping/pong works;
- the board reconnects and sends a fresh hello after a forced close.

Do not retain payloads: TLS keys or decrypted captures contain voice and
transcripts. Record only sizes, ordering, timings, and the redacted header names.
