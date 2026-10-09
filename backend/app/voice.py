"""Transient speech contracts without interview, persistence, or provider state."""

from dataclasses import dataclass, field
from typing import Protocol

from app.interviewer_personas import InterviewerPersonaId

@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    """Validated in-memory MP3; audio is deliberately excluded from repr."""

    audio: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.audio) is not bytes or not self.audio:
            raise ValueError("Speech audio must be nonempty bytes.") from None


class SpeechService(Protocol):
    async def synthesize(
        self, text: str, *, persona_id: InterviewerPersonaId | None = None,
    ) -> SynthesizedSpeech:
        ...


class SpeechUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Voice playback is not configured.")


class SpeechFailed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Voice playback request failed.")


class SpeechTimeout(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Voice playback request timed out.")
