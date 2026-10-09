"""Provider-neutral transient speech contracts."""

from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from app.voice import SpeechFailed, SpeechService, SpeechTimeout, SpeechUnavailable, SynthesizedSpeech


def test_audio_is_immutable_and_never_part_of_repr():
    audio = b"SYNTHETIC_PRIVATE_AUDIO_12749"
    speech = SynthesizedSpeech(audio)
    assert speech.audio is audio
    assert repr(speech) == "SynthesizedSpeech()"
    with pytest.raises(FrozenInstanceError):
        speech.audio = b"replacement"
    with pytest.raises((AttributeError, TypeError)):
        speech.session_id = "forbidden"
    assert not hasattr(speech, "__dict__")


@pytest.mark.parametrize("audio", [None, "audio", bytearray(b"audio"), b""])
def test_audio_requires_nonempty_bytes(audio):
    with pytest.raises(ValueError, match="^Speech audio must be nonempty bytes\\.$"):
        SynthesizedSpeech(audio)


@pytest.mark.parametrize("error_type,message", [
    (SpeechUnavailable, "Voice playback is not configured."),
    (SpeechFailed, "Voice playback request failed."),
    (SpeechTimeout, "Voice playback request timed out."),
])
def test_failure_contracts_have_only_fixed_messages(error_type, message):
    error = error_type()
    assert error.args == (message,)
    assert error.__dict__ == {}
    with pytest.raises(TypeError):
        error_type("SYNTHETIC_PRIVATE_DETAIL")


def test_service_contract_is_text_only_and_returns_transient_speech():
    assert get_type_hints(SpeechService.synthesize) == {"text": str, "return": SynthesizedSpeech}
    assert get_type_hints(SynthesizedSpeech) == {"audio": bytes}
