"""Lazy speech composition does not read configuration or perform work."""

from types import SimpleNamespace

from app.elevenlabs_voice import ElevenLabsSpeechService
from app.voice_composition import get_speech_service


def test_lazy_composition_retains_only_the_stateless_service(monkeypatch):
    from app import elevenlabs_voice as provider

    class ForbiddenEnvironment:
        def get(self, *args, **kwargs):
            raise AssertionError("Factory construction must not read configuration.")

    monkeypatch.setattr(provider, "os", SimpleNamespace(environ=ForbiddenEnvironment()))
    get_speech_service.cache_clear()
    try:
        service = get_speech_service()
        assert type(service) is ElevenLabsSpeechService
        assert get_speech_service() is service
        assert vars(service) == {"_transport": None}
    finally:
        get_speech_service.cache_clear()
