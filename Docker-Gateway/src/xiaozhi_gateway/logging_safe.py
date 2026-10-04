from __future__ import annotations

import hashlib
import json
import logging
import time


def configure(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s")


def device_ref(device_id: str, client_id: str = "") -> str:
    return hashlib.sha256(f"{device_id}\0{client_id}".encode()).hexdigest()[:12]


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    # Callers must never place audio, transcripts, tokens, or keys in fields.
    payload = {"ts": int(time.time()), "event": name, **fields}
    logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))

