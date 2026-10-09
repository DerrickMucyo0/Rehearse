"""Lazy stateless speech composition; configuration is read only on synthesis."""

from functools import lru_cache

from app.elevenlabs_voice import ElevenLabsSpeechService
from app.voice import SpeechService


@lru_cache(maxsize=1)
def get_speech_service() -> SpeechService:
    return ElevenLabsSpeechService()
