"""Lifecycle manager for the local AI compute node."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from urllib.parse import urlsplit

from .config import Config
from .logging_safe import event


class ComputeNodeManager:
    """Wake and wait for the local AI node before a device session starts."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = logging.getLogger("xiaozhi.compute")
        self.socket_path = config.compute_wol_socket
        self.ready_timeout = float(config.compute_ready_timeout_seconds)
        self.poll_seconds = float(config.compute_poll_seconds)

        self.targets = (
            self._target("ollama", config.ollama_base_url),
            self._target("stt", config.local_stt_base_url),
            self._target("tts", config.local_tts_base_url),
        )

        self._state = "off"
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    async def status(self) -> dict[str, object]:
        """Return current lifecycle state without waking the compute node."""
        states = await self._states()

        # A previously-ready node may have been shut down since the last check.
        if self._state == "ready" and not all(states.values()):
            self._state = "off"

        payload: dict[str, object] = {
            "state": self._state,
            **states,
        }
        if self._last_error:
            payload["error"] = self._last_error
        return payload

    def wake(self) -> dict[str, object]:
        """Start wake/readiness orchestration in the background.

        Idempotent: an existing wake task is never duplicated.
        """
        if self._state == "ready":
            return {"state": "ready"}

        if self._task is None or self._task.done():
            self._state = "waking"
            self._last_error = None
            self._task = asyncio.create_task(self._run())

        return {"state": self._state}

    async def _run(self) -> None:
        async def set_state(state: str) -> None:
            self._state = state

        try:
            await self.ensure_ready(set_state)
        except Exception as exc:
            self._state = "error"
            self._last_error = type(exc).__name__
            event(
                self.log,
                "compute_node_error",
                reason=type(exc).__name__,
            )

    @staticmethod
    def _target(name: str, url: str) -> tuple[str, str, int]:
        parts = urlsplit(url)

        if not parts.hostname:
            raise ValueError(f"invalid {name} URL")

        if parts.port:
            port = parts.port
        elif parts.scheme == "https":
            port = 443
        else:
            port = 80

        return name, parts.hostname, port

    @staticmethod
    def _tcp_ready(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    async def _states(self) -> dict[str, bool]:
        checks = [
            asyncio.to_thread(self._tcp_ready, host, port)
            for _, host, port in self.targets
        ]
        results = await asyncio.gather(*checks)

        return {
            name: ready
            for (name, _, _), ready in zip(self.targets, results)
        }

    def _wake(self) -> None:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)

        try:
            client.connect(self.socket_path)
            client.sendall(b"WAKE\n")
            reply = client.recv(32).strip()

            if reply != b"OK":
                raise RuntimeError("WOL helper rejected wake request")
        finally:
            client.close()

    async def _warm_model(self) -> None:
        parts = urlsplit(self.config.ollama_base_url)

        if not parts.hostname:
            raise RuntimeError("invalid Ollama URL")

        port = parts.port or 80
        path = (parts.path.rstrip("/") or "") + "/api/generate"

        body = json.dumps(
            {
                "model": self.config.ollama_model,
                "prompt": "",
                "stream": False,
                "keep_alive": "30m",
            },
            separators=(",", ":"),
        ).encode()

        def request() -> None:
            import http.client

            conn = http.client.HTTPConnection(
                parts.hostname,
                port,
                timeout=float(self.config.ollama_timeout_seconds),
            )
            try:
                conn.request(
                    "POST",
                    path,
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                response.read()

                if response.status != 200:
                    raise RuntimeError(
                        f"Ollama warm-up failed: HTTP {response.status}"
                    )
            finally:
                conn.close()

        event(self.log, "compute_model_warming")
        await asyncio.to_thread(request)
        event(self.log, "compute_model_ready")

    async def ensure_ready(self, status=None) -> None:
        async def report(state: str) -> None:
            if status is not None:
                await status(state)
        started = time.monotonic()
        states = await self._states()

        if all(states.values()):
            await report("warming")
            await self._warm_model()
            await report("ready")
            event(
                self.log,
                "compute_node_ready",
                already_ready=True,
                **states,
            )
            return

        # If nothing responds, assume the machine is off and send WOL.
        # If one service is already reachable, the host is awake and we only
        # wait for the remaining services to finish starting.
        if not any(states.values()):
            await report("waking")
            event(self.log, "compute_node_waking")

            try:
                await asyncio.to_thread(self._wake)
            except OSError as exc:
                raise RuntimeError(
                    f"compute node wake failed: {type(exc).__name__}"
                ) from None

        deadline = time.monotonic() + self.ready_timeout

        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
            states = await self._states()

            if all(states.values()):
                event(
                    self.log,
                    "compute_services_ready",
                    wait_ms=int((time.monotonic() - started) * 1000),
                    **states,
                )

                await report("warming")
                await self._warm_model()
                await report("ready")

                event(
                    self.log,
                    "compute_node_ready",
                    already_ready=False,
                    wait_ms=int((time.monotonic() - started) * 1000),
                    **states,
                )
                return

        event(
            self.log,
            "compute_node_timeout",
            wait_ms=int((time.monotonic() - started) * 1000),
            **states,
        )
        raise RuntimeError("local compute node did not become ready")
