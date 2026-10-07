"""One bounded ElevenLabs TTS request, separate from transcription and diagnosis."""

import asyncio
import os
from urllib.parse import quote

import httpx

from app.voice import SpeechFailed, SpeechTimeout, SpeechUnavailable, SynthesizedSpeech

ELEVENLABS_VOICE_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_VOICE_MODEL = "eleven_multilingual_v2"
ELEVENLABS_VOICE_OUTPUT_FORMAT = "mp3_44100_128"
ELEVENLABS_VOICE_CONNECT_TIMEOUT_SECONDS = 15
ELEVENLABS_VOICE_READ_TIMEOUT_SECONDS = None
ELEVENLABS_VOICE_WRITE_TIMEOUT_SECONDS = 15
ELEVENLABS_VOICE_POOL_TIMEOUT_SECONDS = 15
ELEVENLABS_VOICE_TOTAL_TIMEOUT_SECONDS = 60
ELEVENLABS_VOICE_MAX_BYTES = 2 * 1024 * 1024

# MPEG audio bitrate tables, indexed by the four-bit bitrate index. A zero
# bitrate (free format) cannot supply a deterministically bounded frame length.
_MPEG1_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG2_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _plausible_mp3(audio: bytes) -> bool:
    """Check a bounded ID3 tag and complete MPEG frame, not decode or repair audio."""
    offset = 0
    if audio.startswith(b"ID3"):
        if len(audio) < 10 or audio[3] not in (2, 3, 4) or audio[4] == 255:
            return False
        if audio[5] & {2: 0x3F, 3: 0x1F, 4: 0x0F}[audio[3]]:
            return False
        size_bytes = audio[6:10]
        if any(value & 128 for value in size_bytes):
            return False
        tag_size = sum(value << shift for value, shift in zip(size_bytes, (21, 14, 7, 0)))
        offset = 10 + tag_size
        # ID3v2.4 may carry a footer after the declared metadata region.
        if audio[3] == 4 and audio[5] & 16:
            if audio[offset:offset + 10] != b"3DI" + audio[3:10]:
                return False
            offset += 10
    if len(audio) < offset + 4:
        return False
    header = int.from_bytes(audio[offset:offset + 4], "big")
    if header >> 21 != 0x7FF:
        return False
    version = (header >> 19) & 3
    layer = (header >> 17) & 3
    bitrate_index = (header >> 12) & 15
    sample_index = (header >> 10) & 3
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_index == 3:
        return False
    # Emphasis value 2 is reserved in all MPEG audio versions.
    if header & 3 == 2:
        return False
    rates = (44100, 48000, 32000)
    sample_rate = rates[sample_index] // ({3: 1, 2: 2, 0: 4}[version])
    table = _MPEG1_BITRATES if version == 3 else _MPEG2_BITRATES
    bitrate = table[bitrate_index] * 1000
    padding = (header >> 9) & 1
    coefficient = 144 if version == 3 else 72
    frame_size = coefficient * bitrate // sample_rate + padding
    return frame_size >= 4 and len(audio) >= offset + frame_size


async def _bounded_audio(response: httpx.Response) -> bytes | None:
    if response.status_code != 200:
        return None
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "audio/mpeg":
        return None
    # Request identity encoding and reject unexpected compression so the byte
    # cap applies to actual downloaded MP3 bytes as well as returned audio.
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        return None
    content_length = response.headers.get("content-length", "")
    if content_length.isascii() and content_length.isdecimal():
        if int(content_length) > ELEVENLABS_VOICE_MAX_BYTES:
            return None
    audio = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > ELEVENLABS_VOICE_MAX_BYTES - len(audio):
            return None
        audio.extend(chunk)
    result = bytes(audio)
    return result if result and _plausible_mp3(result) else None


class ElevenLabsSpeechService:
    """Runtime key/voice configuration; only exact question text reaches TTS."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
        if not key or not voice_id:
            raise SpeechUnavailable() from None
        audio = None
        try:
            async with asyncio.timeout(ELEVENLABS_VOICE_TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=httpx.Timeout(
                        connect=ELEVENLABS_VOICE_CONNECT_TIMEOUT_SECONDS,
                        read=ELEVENLABS_VOICE_READ_TIMEOUT_SECONDS,
                        write=ELEVENLABS_VOICE_WRITE_TIMEOUT_SECONDS,
                        pool=ELEVENLABS_VOICE_POOL_TIMEOUT_SECONDS,
                    ),
                    follow_redirects=False,
                ) as http:
                    async with http.stream(
                        "POST", f"{ELEVENLABS_VOICE_ENDPOINT}/{quote(voice_id, safe='')}",
                        params={"output_format": ELEVENLABS_VOICE_OUTPUT_FORMAT},
                        headers={
                            "xi-api-key": key, "Accept": "audio/mpeg", "Accept-Encoding": "identity",
                        },
                        json={"text": text, "model_id": ELEVENLABS_VOICE_MODEL},
                    ) as response:
                        audio = await _bounded_audio(response)
        except (httpx.TimeoutException, TimeoutError):
            timed_out = True
        except Exception:
            timed_out = False
        else:
            if audio is not None:
                return SynthesizedSpeech(audio)
            timed_out = False
        # Raise outside handlers so upstream bodies/exception details are not
        # retained as implicit exception context. Caller cancellation propagates.
        if timed_out:
            raise SpeechTimeout() from None
        raise SpeechFailed() from None
