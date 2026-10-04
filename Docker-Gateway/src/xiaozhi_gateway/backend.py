"""Provider-neutral conversation backend contract.

The XiaoZhi protocol layer talks only to this interface. A backend hides how
speech recognition, the language model and speech synthesis are provided: one
realtime model, or separate STT/LLM/TTS stages.

Audio contract
- input:  PCM16 little-endian, mono, 16 kHz (as decoded from the device)
- output: PCM16 little-endian, mono, 24 kHz (as encoded for the device)

Turn contract
- The session owns the turn counter and passes it to ``start_turn`` and
  ``send_text``. A backend tags every response event with the turn it belongs
  to, so the session can drop output that arrives after an abort.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import Config


class BackendError(RuntimeError):
    """A backend failure. The message must not contain provider payloads."""


@dataclass(frozen=True, slots=True)
class UserSpeechStarted:
    """The backend detected the start of user speech."""


@dataclass(frozen=True, slots=True)
class UserSpeechEnded:
    """The backend decided that the user's turn has ended."""


@dataclass(frozen=True, slots=True)
class UserTranscript:
    """Final recognised text of the user's speech."""

    text: str


@dataclass(frozen=True, slots=True)
class ResponseStarted:
    turn: int


@dataclass(frozen=True, slots=True)
class ResponseText:
    """A fragment of the text that is being spoken.

    ``final`` marks the last fragment: the answer text is complete, even if
    its audio is still being produced.
    """

    turn: int
    text: str
    final: bool = False


@dataclass(frozen=True, slots=True)
class ResponseAudio:
    """PCM16 mono 24 kHz audio of any length."""

    turn: int
    pcm: bytes


@dataclass(frozen=True, slots=True)
class ResponseDone:
    turn: int


BackendEvent = (
    UserSpeechStarted
    | UserSpeechEnded
    | UserTranscript
    | ResponseStarted
    | ResponseText
    | ResponseAudio
    | ResponseDone
)


class ConversationBackend(Protocol):
    async def open(self) -> None: ...

    async def start_turn(self, turn: int, mode: str) -> None:
        """Begin capturing a user turn. Mode is auto, manual or realtime.

        In auto and realtime mode the backend ends the turn itself and reports
        it with ``UserSpeechEnded``. In manual mode it waits for ``end_turn``.
        """

    async def append_audio(self, pcm16k: bytes) -> None: ...

    async def end_turn(self) -> None:
        """The device ended the turn explicitly; respond to the audio so far."""

    async def send_text(self, turn: int, text: str) -> None:
        """Respond to a text-only user turn (for example a wake word)."""

    async def cancel(self, *, response: bool, clear_audio: bool) -> None:
        """Stop the current turn: its response and/or its buffered input."""

    def events(self) -> AsyncIterator[BackendEvent]:
        """Yield normalised events; raise ``BackendError`` on failure."""

    async def close(self) -> None: ...


BACKENDS = ("openai", "mistral")


def create_backend(config: "Config", identity: str) -> ConversationBackend:
    name = getattr(config, "backend", "openai")
    if name == "openai":
        from .openai_backend import OpenAIBackend

        return OpenAIBackend(config, identity)
    if name == "mistral":
        from .mistral_backend import MistralBackend

        return MistralBackend(config, identity)
    raise BackendError(f"unknown backend {name!r}")
