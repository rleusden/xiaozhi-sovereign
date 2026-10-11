from __future__ import annotations

import asyncio
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

            # Compute readiness is a gateway capability, not necessarily a
            # physical compute-node capability. Cloud backends such as OpenAI
            # and Mistral do not require the local Fujitsu node, so they are
            # immediately ready from the device's point of view.
            #
            # This deliberately keeps the ESP firmware backend-agnostic:
            #     GET  /compute/status -> {"state":"ready"}
            #     POST /compute/wake   -> {"state":"ready"}
            #
            # The existing local-backend handlers below remain responsible for
            # off/waking/warming/ready and WOL.
            if self.config.backend != "local":
                if method == "GET" and path == "/compute/status":
                    await self._respond(
                        writer,
                        HTTPStatus.OK,
                        {"state": "ready"},
                    )
                    return

                if method == "POST" and path == "/compute/wake":
                    await self._respond(
                        writer,
                        HTTPStatus.OK,
                        {"state": "ready"},
                    )
                    return
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

            elif method == "GET" and path == "/compute/status":
                if self.compute_node is None:
                    await self._respond(
                        writer,
                        HTTPStatus.NOT_FOUND,
                        {"state": "unavailable"},
                    )
                else:
                    await self._respond(
                        writer,
                        HTTPStatus.OK,
                        await self.compute_node.status(),
                    )

            elif method == "POST" and path == "/compute/wake":
                if self.compute_node is None:
                    await self._respond(
                        writer,
                        HTTPStatus.NOT_FOUND,
                        {"state": "unavailable"},
                    )
                else:
                    await self._respond(
                        writer,
                        HTTPStatus.ACCEPTED,
                        self.compute_node.wake(),
                    )

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
