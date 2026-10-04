from __future__ import annotations

import hashlib
import json
import logging
import time


def configure(level: str) -> None:
    # Protocol diagnostics include Authorization headers and message bodies.
    # Keep application DEBUG available without enabling WebSocket wire logs.
    wire_log = logging.getLogger("websockets")
    wire_log.handlers.clear()
    wire_log.addHandler(logging.NullHandler())
    wire_log.propagate = False
    wire_log.setLevel(logging.CRITICAL + 1)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("websockets.") and isinstance(logger, logging.Logger):
            logger.handlers.clear()
            logger.propagate = True
            logger.setLevel(logging.NOTSET)
    logging.basicConfig(level=level, format="%(message)s")


def device_ref(device_id: str, client_id: str = "") -> str:
    return hashlib.sha256(f"{device_id}\0{client_id}".encode()).hexdigest()[:12]


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    # Callers must never place audio, transcripts, tokens, or keys in fields.
    payload = {"ts": int(time.time()), "event": name, **fields}
    logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))
