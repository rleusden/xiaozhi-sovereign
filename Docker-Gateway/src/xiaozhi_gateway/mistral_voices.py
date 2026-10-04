"""List the Mistral voices available to the configured API key.

Usage inside the container:
    python -m xiaozhi_gateway.mistral_voices
Prints one voice per line: id, type, languages, name. Put the chosen id in
MISTRAL_VOICE_ID.
"""

from __future__ import annotations

import asyncio
import json
import os

from .config import _secret
from .http_client import HttpClient, HttpError


async def main() -> None:
    key = _secret(
        os.getenv("MISTRAL_API_KEY_FILE", "/run/secrets/mistral_api_key"),
        "Mistral API key",
    )
    http = HttpClient(
        os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai"),
        {"Authorization": f"Bearer {key}"},
    )
    try:
        offset = 0
        while True:
            data = json.loads(
                await http.request("GET", f"/v1/audio/voices?limit=100&offset={offset}")
            )
            items = data.get("items") or []
            for voice in items:
                languages = ",".join(voice.get("languages") or []) or "-"
                print(f"{voice.get('id')}  {voice.get('type')}  {languages}  {voice.get('name')}")
            offset += len(items)
            if not items or offset >= int(data.get("total") or 0):
                break
    except HttpError as exc:
        raise SystemExit(f"Mistral voice list failed: HTTP {exc.status}")
    finally:
        http.close()


if __name__ == "__main__":
    asyncio.run(main())
