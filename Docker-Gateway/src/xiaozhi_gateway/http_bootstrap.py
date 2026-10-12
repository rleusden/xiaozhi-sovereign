from __future__ import annotations

import asyncio
import hmac
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from http import HTTPStatus

from .config import Config
from .compute_node import ComputeNodeManager


class BootstrapServer:
    HEADER_LIMIT = 16 * 1024
    BODY_LIMIT = 64 * 1024

    def __init__(
        self,
        config: Config,
        compute_node: ComputeNodeManager | None = None,
    ) -> None:
        self.config = config
        self.compute_node = compute_node

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(request) > self.HEADER_LIMIT:
                raise ValueError("headers too large")
            head = request.decode("iso-8859-1").split("\r\n")
            method, path, _ = head[0].split(" ", 2)

            headers: dict[str, str] = {}
            for line in head[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                headers[key.casefold()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length < 0 or length > self.BODY_LIMIT:
                raise ValueError("body too large")
            if length:
                await asyncio.wait_for(reader.readexactly(length), timeout=5)
            if method == "GET" and path in {"/healthz", "/readyz"}:
                await self._respond(writer, HTTPStatus.OK, {"status": "ok"})

            elif path in {"/compute/status", "/compute/wake"}:
                await self._compute(writer, method, path, headers)

            elif method in {"GET", "POST"} and path == "/xiaozhi/ota/":
                await self._respond(
                    writer,
                    HTTPStatus.OK,
                    {
                        "websocket": {
                            "url": self.config.public_ws_url,
                            "token": self.config.device_token,
                            "version": 1,
                        },
                        "compute": self._compute_bootstrap(),
                        "server_time": {
                            "timestamp": int(time.time() * 1000),
                            "timezone_offset": int(
                                datetime.now(
                                    ZoneInfo(self.config.timezone)
                                ).utcoffset().total_seconds() // 60
                            ),
                        },
                    },
                )
            else:
                await self._respond(writer, HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await self._respond(writer, HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        except TimeoutError:
            await self._respond(writer, HTTPStatus.REQUEST_TIMEOUT, {"error": "timeout"})
        finally:
            writer.close()
            await writer.wait_closed()

    def _compute_bootstrap(self) -> dict[str, object]:
        """Tell the device whether, and where, to check compute readiness.

        Only the local backend needs a compute node that may be off. With a
        cloud backend the device skips the readiness check altogether.
        """
        base = self.config.public_compute_url
        return {
            "required": self.config.backend == "local",
            "status_url": f"{base}/compute/status",
            "wake_url": f"{base}/compute/wake",
        }

    def _authorized(self, headers: dict[str, str]) -> bool:
        """Same credential as the WebSocket: device token, optional device ID."""
        presented = headers.get("authorization", "")
        if not hmac.compare_digest(presented, f"Bearer {self.config.device_token}"):
            return False
        allowed = self.config.allowed_device_id
        if allowed:
            device = headers.get("device-id", "")
            if not hmac.compare_digest(device.casefold(), allowed.casefold()):
                return False
        return True

    async def _compute(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
    ) -> None:
        expected = "GET" if path == "/compute/status" else "POST"
        if method != expected:
            await self._respond(writer, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})
            return
        # Waking the compute node costs energy and can be abused, so these
        # endpoints require the device credential, like the WebSocket does.
        if not self._authorized(headers):
            await self._respond(writer, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if self.config.backend != "local":
            # A cloud backend needs no compute node: it is always ready.
            await self._respond(writer, HTTPStatus.OK, {"state": "ready"})
            return
        if self.compute_node is None:
            await self._respond(writer, HTTPStatus.NOT_FOUND, {"state": "unavailable"})
        elif path == "/compute/status":
            await self._respond(writer, HTTPStatus.OK, await self.compute_node.status())
        else:
            await self._respond(writer, HTTPStatus.ACCEPTED, self.compute_node.wake())

    async def _respond(
        self, writer: asyncio.StreamWriter, status: HTTPStatus, payload: dict
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        header = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()
