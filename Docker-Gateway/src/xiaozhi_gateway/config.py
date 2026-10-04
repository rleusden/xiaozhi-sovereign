from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _secret(path: str, label: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read {label} secret file") from exc
    if not value:
        raise RuntimeError(f"{label} secret is empty")
    return value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    bind_host: str
    bootstrap_port: int
    websocket_port: int
    public_ws_url: str
    device_token: str
    openai_api_key: str
    openai_model: str
    transcription_model: str
    voice: str
    instructions: str
    allowed_device_id: str | None
    max_turn_seconds: int
    max_session_seconds: int
    timezone_offset_minutes: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        public_url = os.getenv("PUBLIC_WS_URL", "").strip()
        if not public_url.startswith("wss://"):
            raise RuntimeError("PUBLIC_WS_URL must start with wss://")
        return cls(
            bind_host=os.getenv("BIND_HOST", "0.0.0.0"),
            bootstrap_port=_integer("BOOTSTRAP_PORT", 8080, 1, 65535),
            websocket_port=_integer("WEBSOCKET_PORT", 8081, 1, 65535),
            public_ws_url=public_url,
            device_token=_secret(
                os.getenv("DEVICE_TOKEN_FILE", "/run/secrets/xiaozhi_device_token"),
                "device token",
            ),
            openai_api_key=_secret(
                os.getenv("OPENAI_API_KEY_FILE", "/run/secrets/openai_api_key"),
                "OpenAI API key",
            ),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-realtime-2.1"),
            transcription_model=os.getenv(
                "OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"
            ),
            voice=os.getenv("OPENAI_VOICE", "marin"),
            instructions=os.getenv(
                "OPENAI_INSTRUCTIONS",
                "Antwoord in het Nederlands, tenzij de gebruiker een andere taal gebruikt. "
                "Beperk ieder antwoord tot maximaal drie korte zinnen en circa 35 woorden. "
                "Geef direct antwoord en vraag niet standaard of de gebruiker nog meer wil weten. "
                "Doe geen alsof-beloftes over acties die je niet kunt uitvoeren.",
            ),
            allowed_device_id=os.getenv("XIAOZHI_ALLOWED_DEVICE_ID") or None,
            max_turn_seconds=_integer("MAX_TURN_SECONDS", 60, 5, 300),
            max_session_seconds=_integer("MAX_SESSION_SECONDS", 3300, 60, 3600),
            timezone_offset_minutes=_integer(
                "TIMEZONE_OFFSET_MINUTES", 0, -720, 840
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
