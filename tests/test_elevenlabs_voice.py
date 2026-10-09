"""Bounded offline TTS contracts; every request uses synthetic in-memory transport."""

import asyncio
import json
import traceback
from types import SimpleNamespace

import httpx
import pytest

from app import elevenlabs_voice as provider
from app.voice import SpeechFailed, SpeechTimeout, SpeechUnavailable, SynthesizedSpeech

KEY = "SYNTHETIC_PRIVATE_TTS_KEY_59306"
VOICE = "synthetic-voice-79365"
QUESTION = " \tExact persisted synthetic question? 中文 😀\n "
PRIVATE_DETAIL = "SYNTHETIC_PRIVATE_PROVIDER_DETAIL_98712"
FRAME = bytes.fromhex("fffb9000") + bytes(413)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    monkeypatch.setattr(provider, "os", SimpleNamespace(environ={
        "ELEVENLABS_API_KEY": KEY, "ELEVENLABS_VOICE_ID": VOICE,
    }))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Live provider transport is forbidden.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def run_response(*, audio=FRAME, status=200, media_type="audio/mpeg", headers=None):
    requests = []
    stream = Chunks([audio])

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Content-Type": media_type, **(headers or {})}, stream=stream)

    service = provider.ElevenLabsSpeechService(httpx.MockTransport(respond))
    return service, requests, stream


def synthesize(service, text=QUESTION):
    return asyncio.run(service.synthesize(text))


def assert_sanitized(error, expected_type, capsys, caplog):
    assert type(error) is expected_type
    assert error.__cause__ is None and error.__context__ is None
    assert error.__dict__ == {}
    public = str(error) + repr(error) + "".join(traceback.format_exception(error))
    for value in (KEY, VOICE, QUESTION, PRIVATE_DETAIL):
        assert value not in public
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_exact_text_request_fixed_model_format_single_post_and_closed_stream():
    service, requests, stream = run_response()
    result = synthesize(service)
    assert type(result) is SynthesizedSpeech
    assert result.audio == FRAME
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_44100_128"
    assert request.headers["xi-api-key"] == KEY
    assert request.headers["accept"] == "audio/mpeg"
    assert request.headers["accept-encoding"] == "identity"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {"text": QUESTION, "model_id": "eleven_multilingual_v2"}
    assert request.extensions["timeout"] == {"connect": 15, "read": None, "write": 15, "pool": 15}
    assert stream.closed
    assert not hasattr(service, "key") and not hasattr(service, "voice_id")


@pytest.mark.parametrize("name", ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"])
@pytest.mark.parametrize("value", [None, "", " \t\r\n"])
def test_missing_runtime_configuration_stops_before_http(name, value, monkeypatch, capsys, caplog):
    if value is None:
        del provider.os.environ[name]
    else:
        provider.os.environ[name] = value

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing configuration must not construct a client.")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(SpeechUnavailable) as caught:
        synthesize(provider.ElevenLabsSpeechService())
    assert_sanitized(caught.value, SpeechUnavailable, capsys, caplog)


def test_configuration_is_read_at_request_time_and_voice_is_one_encoded_path(monkeypatch):
    provider.os.environ.clear()
    service, requests, _ = run_response()
    provider.os.environ.update({"ELEVENLABS_API_KEY": f" {KEY}\n", "ELEVENLABS_VOICE_ID": " /voice?part#fragment "})
    assert synthesize(service).audio == FRAME
    assert requests[0].headers["xi-api-key"] == KEY
    assert "/%2Fvoice%3Fpart%23fragment?output_format=" in str(requests[0].url)


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 400, 401, 403, 429, 500, 503])
def test_provider_errors_redirects_and_sensitive_bodies_are_discarded(status, capsys, caplog):
    service, requests, stream = run_response(
        status=status, audio=PRIVATE_DETAIL.encode(),
        headers={"Location": "https://provider-secret.example.test/private", "X-Private": PRIVATE_DETAIL},
    )
    with pytest.raises(SpeechFailed) as caught:
        synthesize(service)
    assert len(requests) == 1
    assert stream.reads == 0 and stream.closed
    assert_sanitized(caught.value, SpeechFailed, capsys, caplog)


@pytest.mark.parametrize("media_type", ["audio/wav", "application/json", "text/html", "", "audio/mp3"])
def test_non_mp3_media_types_rejected_before_body_download(media_type):
    service, _, stream = run_response(media_type=media_type)
    with pytest.raises(SpeechFailed):
        synthesize(service)
    assert stream.reads == 0 and stream.closed


@pytest.mark.parametrize("media_type", ["audio/mpeg", " AUDIO/MPEG ; charset=binary"])
def test_normalized_audio_mpeg_is_accepted(media_type):
    service, _, _ = run_response(media_type=media_type)
    assert synthesize(service).audio == FRAME


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
def test_unexpected_compression_is_rejected_without_reading_the_body(encoding):
    service, _, stream = run_response(headers={"Content-Encoding": encoding})
    with pytest.raises(SpeechFailed):
        synthesize(service)
    assert stream.reads == 0 and stream.closed


def test_declared_size_only_an_early_bound_and_actual_bytes_remain_authoritative():
    service, requests, stream = run_response(headers={"Content-Length": str(provider.ELEVENLABS_VOICE_MAX_BYTES + 1)})
    with pytest.raises(SpeechFailed):
        synthesize(service)
    assert len(requests) == 1 and stream.reads == 0 and stream.closed
    service, _, _ = run_response(headers={"Content-Length": "0"})
    assert synthesize(service).audio == FRAME


@pytest.mark.parametrize("content_length", [None, "1", "invalid"])
def test_download_limit_applies_without_a_trustworthy_content_length(content_length):
    chunks = Chunks([FRAME, bytes(provider.ELEVENLABS_VOICE_MAX_BYTES), PRIVATE_DETAIL.encode()])
    requests = []

    def respond(request):
        requests.append(request)
        headers = {"Content-Type": "audio/mpeg"}
        if content_length is not None:
            headers["Content-Length"] = content_length
        return httpx.Response(200, headers=headers, stream=chunks)

    with pytest.raises(SpeechFailed):
        synthesize(provider.ElevenLabsSpeechService(httpx.MockTransport(respond)))
    assert len(requests) == 1 and chunks.reads == 2 and chunks.closed


def test_exact_maximum_is_valid_without_rounding_or_reconstruction():
    audio = FRAME + bytes(provider.ELEVENLABS_VOICE_MAX_BYTES - len(FRAME))
    service, _, stream = run_response(audio=audio)
    assert synthesize(service).audio == audio
    assert stream.closed


@pytest.mark.parametrize("audio", [
    b"", b"{\"error\":\"private\"}", b"<html>private</html>", b"ID3", b"\xff\xfb\x90\x00", FRAME[:-1],
    b"ID3\x03\x00\x00\x00\x00\x00\x00", b"ID3\xff\x00\x00\x00\x00\x00\x00" + FRAME,
    b"ID3\x03\x00\x00\x80\x00\x00\x00" + FRAME,
    b"ID3\x03\x00\x00\x00\x00\x01\x00" + FRAME,
    b"ID3\x03\x00\x01\x00\x00\x00\x00" + FRAME,
    b"ID3\x04\x00\x10\x00\x00\x00\x00" + FRAME,
    bytes.fromhex("ffeb9000") + bytes(500),  # Reserved MPEG version.
    bytes.fromhex("fff99000") + bytes(500),  # Reserved layer.
    bytes.fromhex("fffb0000") + bytes(500),  # Unbounded free bitrate.
    bytes.fromhex("fffbf000") + bytes(500),  # Reserved bitrate.
    bytes.fromhex("fffb9c00") + bytes(500),  # Reserved sample rate.
    bytes.fromhex("fffb9002") + bytes(500),  # Reserved emphasis.
])
def test_empty_error_content_malformed_id3_and_invalid_mpeg_rejected(audio):
    service, _, stream = run_response(audio=audio)
    with pytest.raises(SpeechFailed):
        synthesize(service)
    assert stream.closed


@pytest.mark.parametrize("audio", [
    FRAME,
    bytes.fromhex("fff38000") + bytes(204),  # MPEG2 layer III, 64 kbps / 22050 Hz.
    bytes.fromhex("ffe38000") + bytes(413),  # MPEG2.5 layer III, 64 kbps / 11025 Hz.
    b"ID3\x02\x00\x00\x00\x00\x00\x03abc" + FRAME,
    b"ID3\x03\x00\x00\x00\x00\x00\x00" + FRAME,
    b"ID3\x04\x00\x10\x00\x00\x00\x00" + b"3DI\x04\x00\x10\x00\x00\x00\x00" + FRAME,
])
def test_id3_metadata_and_multiple_mpeg_versions_are_plausible_mp3(audio):
    service, _, _ = run_response(audio=audio)
    assert synthesize(service).audio == audio


@pytest.mark.parametrize("error_type", [
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, TimeoutError,
])
def test_timeouts_are_fixed_sanitized_single_request_failures(error_type, capsys, caplog):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        raise error_type(PRIVATE_DETAIL)

    with pytest.raises(SpeechTimeout) as caught:
        synthesize(provider.ElevenLabsSpeechService(httpx.MockTransport(respond)))
    assert calls == 1
    assert_sanitized(caught.value, SpeechTimeout, capsys, caplog)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.RemoteProtocolError, RuntimeError])
def test_transport_and_unexpected_errors_never_retain_exception_details(error_type, capsys, caplog):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        raise error_type(PRIVATE_DETAIL)

    with pytest.raises(SpeechFailed) as caught:
        synthesize(provider.ElevenLabsSpeechService(httpx.MockTransport(respond)))
    assert calls == 1
    assert_sanitized(caught.value, SpeechFailed, capsys, caplog)


def test_total_deadline_covers_body_download_without_sleep(monkeypatch, capsys, caplog):
    calls = 0
    seen = []
    real_timeout = asyncio.timeout

    class Waiting(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = Waiting()

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, stream=stream)

    def timeout(seconds):
        seen.append(seconds)
        return real_timeout(0)

    monkeypatch.setattr(provider.asyncio, "timeout", timeout)
    with pytest.raises(SpeechTimeout) as caught:
        synthesize(provider.ElevenLabsSpeechService(httpx.MockTransport(respond)))
    assert seen == [60] and calls == 1 and stream.closed
    assert_sanitized(caught.value, SpeechTimeout, capsys, caplog)


def test_caller_cancellation_propagates_and_closes_body_without_retry():
    calls = 0
    entered = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    stream = Waiting()

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, stream=stream)

    async def run():
        task = asyncio.create_task(provider.ElevenLabsSpeechService(httpx.MockTransport(respond)).synthesize(QUESTION))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls == 1 and stream.closed
