"""Minimal asyncio HTTP client on the standard library.

Requests run on worker threads with ``http.client``; idle keep-alive
connections are reused so that a turn does not pay several TLS handshakes.
Only what the gateway needs is implemented: a buffered request and a
line-streamed POST (server-sent events). Response bodies of failed requests
are never surfaced, only the status code.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import threading
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

_STALE = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)


class HttpError(Exception):
    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _error(response: http.client.HTTPResponse) -> HttpError:
    try:
        retry_after = float(response.getheader("Retry-After", ""))
    except ValueError:
        retry_after = None
    return HttpError(response.status, retry_after)


class _Cancelled(Exception):
    pass


class _Call:
    """One in-flight request, so that it can be interrupted from the loop."""

    def __init__(self) -> None:
        self.conn: http.client.HTTPConnection | None = None
        self.cancelled = False
        self.finished = False

    def cancel(self) -> None:
        self.cancelled = True
        conn = self.conn
        if self.finished or conn is None or conn.sock is None:
            return
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class HttpClient:
    MAX_IDLE = 4

    def __init__(self, base_url: str, headers: dict[str, str], timeout: float = 30) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("base URL must be http(s)://host[:port]")
        self._secure = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._headers = dict(headers)
        self._timeout = timeout
        self._idle: list[http.client.HTTPConnection] = []
        self._lock = threading.Lock()
        self._closed = False

    # -- public --------------------------------------------------------------

    async def warm(self) -> None:
        """Open one connection in advance; failures surface on first use."""

        def connect() -> None:
            conn = self._new()
            try:
                conn.connect()
            except OSError:
                conn.close()
                return
            self._release(conn)

        await asyncio.to_thread(connect)

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        call = _Call()

        def run() -> bytes:
            try:
                response = self._send(call, method, path, body, headers)
                data = response.read()
            except BaseException:
                call.finished = True
                if call.conn is not None:
                    call.conn.close()
                raise
            self._finish(call, response)
            if response.status != 200:
                raise _error(response)
            return data

        try:
            return await asyncio.to_thread(run)
        finally:
            call.cancel()

    async def stream_lines(
        self, path: str, *, body: bytes, headers: dict[str, str] | None = None
    ) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        call = _Call()

        def push(item: bytes | BaseException | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # event loop already closed

        def run() -> None:
            try:
                response = self._send(call, "POST", path, body, headers)
                if response.status != 200:
                    response.read()
                    self._finish(call, response)
                    raise _error(response)
                while True:
                    line = response.readline()
                    if not line:
                        break
                    push(line)
                self._finish(call, response)
                push(None)
            except BaseException as exc:
                call.finished = True
                if call.conn is not None:
                    call.conn.close()
                push(exc)

        worker = loop.run_in_executor(None, run)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            call.cancel()
            worker.cancel()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    # -- worker-thread helpers -----------------------------------------------

    def _new(self) -> http.client.HTTPConnection:
        factory = (
            http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        )
        return factory(self._host, self._port, timeout=self._timeout)

    def _acquire(self) -> tuple[http.client.HTTPConnection, bool]:
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._new(), False

    def _release(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            if not self._closed and len(self._idle) < self.MAX_IDLE:
                self._idle.append(conn)
                return
        conn.close()

    def _send(
        self,
        call: _Call,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> http.client.HTTPResponse:
        merged = {**self._headers, **(headers or {})}
        while True:
            conn, reused = self._acquire()
            call.conn = conn
            if call.cancelled:
                conn.close()
                raise _Cancelled
            try:
                conn.request(method, self._prefix + path, body=body, headers=merged)
                return conn.getresponse()
            except _STALE:
                conn.close()
                if not reused:
                    raise
                # An idle keep-alive connection was closed by the server.

    def _finish(self, call: _Call, response: http.client.HTTPResponse) -> None:
        call.finished = True
        conn = call.conn
        if conn is None:
            return
        if response.will_close or call.cancelled:
            conn.close()
        else:
            self._release(conn)
