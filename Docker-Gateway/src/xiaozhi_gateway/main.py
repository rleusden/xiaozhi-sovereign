from __future__ import annotations

import asyncio
import logging
import signal

from websockets.asyncio.server import serve

from .config import Config
from .http_bootstrap import BootstrapServer
from .logging_safe import configure, event
from .protocol import DeviceSession


async def main() -> None:
    config = Config.from_env()
    configure(config.log_level)
    logger = logging.getLogger("xiaozhi")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    bootstrap = BootstrapServer(config)
    http_server = await asyncio.start_server(
        bootstrap.handle,
        config.bind_host,
        config.bootstrap_port,
        limit=BootstrapServer.HEADER_LIMIT + 1,
    )

    async def handler(ws):
        await DeviceSession(ws, config).run()

    async with serve(
        handler,
        config.bind_host,
        config.websocket_port,
        compression=None,
        max_size=BootstrapServer.BODY_LIMIT,
        max_queue=16,
        ping_interval=30,
        ping_timeout=10,
        close_timeout=5,
    ):
        event(
            logger,
            "gateway_ready",
            bootstrap_port=config.bootstrap_port,
            websocket_port=config.websocket_port,
        )
        await stop.wait()
    http_server.close()
    await http_server.wait_closed()
    event(logger, "gateway_stopped")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()

