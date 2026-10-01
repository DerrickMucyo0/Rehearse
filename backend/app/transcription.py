"""Provider-independent transcription contract and ElevenLabs adapter."""
import asyncio
import os
import sys
from typing import Annotated, Protocol

import httpx
from elevenlabs.client import AsyncElevenLabs
from elevenlabs.core.api_error import ApiError
from elevenlabs.core.parse_error import ParsingError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.datastructures import UploadFile

PROVIDER_TIMEOUT_SECONDS = 60

# Provider values are used only as lookup keys, never as log arguments.
_ERROR_CATEGORIES = {
    "authentication_error": "authentication", "invalid_api_key": "authentication",
    "authorization_error": "authorization", "missing_permissions": "authorization",
    "payment_required": "payment_or_quota", "quota_exceeded": "payment_or_quota",
    "validation_error": "validation", "invalid_request": "validation",
    "invalid_parameters": "validation", "invalid_content": "validation",
    "rate_limit_error": "rate_limit", "rate_limit_exceeded": "rate_limit",
    "concurrent_limit_exceeded": "rate_limit",
    "internal_error": "provider_server_error", "service_unavailable": "provider_server_error",
}


def _log_failure(stage: str, error: Exception) -> None:
    if os.environ.get("REHEARSE_TRANSCRIPTION_DEBUG") != "1":
        return
    status = None
    category = "unknown_provider_error"
    if isinstance(error, (ApiError, ParsingError)):
        if type(error.status_code) is int and 100 <= error.status_code <= 599:
            status = error.status_code
    if isinstance(error, ApiError):
        detail = error.body.get("detail") if isinstance(error.body, dict) else None
        if isinstance(detail, dict):
            for field in ("code", "status", "type"):
                value = detail.get(field)
                if type(value) is str and value in _ERROR_CATEGORIES:
                    category = _ERROR_CATEGORIES[value]
                    break
        if category == "unknown_provider_error":
            category = {401: "authentication", 402: "payment_or_quota", 403: "authorization",
                        400: "validation", 422: "validation", 429: "rate_limit"}.get(status, category)
            if status is not None and status >= 500:
                category = "provider_server_error"
    elif isinstance(error, (httpx.RequestError, TimeoutError)):
        category = "network_error"
    if isinstance(error, ParsingError):
        stage = "result_mapping"
    if stage == "result_mapping":
        category = "validation"
    # Fixed fields only: no exception formatting, traceback, headers, or body.
    # Explicit development-only stderr output: independent of root/Uvicorn handlers.
    safe_stage = "result_mapping" if stage == "result_mapping" else "provider_request"
    safe_status = status if status is not None else "unavailable"
    print(f"transcription_failure stage={safe_stage} provider_status={safe_status} category={category}",
          file=sys.stderr, flush=True)


class WordTiming(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    text: Annotated[str, Field(min_length=1)]
    start: Annotated[float, Field(ge=0)]
    end: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Invalid timing interval")
        return self


class TranscriptionResult(BaseModel):
    model_config = ConfigDict(strict=True)
    text: Annotated[str, Field(min_length=1, max_length=10000)]
    language: str | None = None
    words: list[WordTiming] = Field(default_factory=list)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.text.strip():
            raise ValueError("Empty transcript")
        return self


class TranscriptionUnavailable(Exception):
    pass


class TranscriptionFailed(Exception):
    pass


class TranscriptionTimeout(Exception):
    pass


class TranscriptionService(Protocol):
    async def transcribe(self, audio: UploadFile, filename: str) -> TranscriptionResult: ...


class ElevenLabsTranscriptionService:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def transcribe(self, audio: UploadFile, filename: str) -> TranscriptionResult:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not key:
            raise TranscriptionUnavailable() from None
        # Shared only with this request's wait_for task, never with other requests.
        stage = ["provider_request"]
        try:
            return await asyncio.wait_for(self._convert(audio, filename, key, stage), PROVIDER_TIMEOUT_SECONDS)
        except (TimeoutError, httpx.TimeoutException) as error:
            _log_failure(stage[0], error)
            raise TranscriptionTimeout() from None
        except Exception as error:
            _log_failure(stage[0], error)
            raise TranscriptionFailed() from None

    async def _convert(self, audio: UploadFile, filename: str, key: str, stage: list[str]) -> TranscriptionResult:
        async with httpx.AsyncClient(transport=self._transport, timeout=PROVIDER_TIMEOUT_SECONDS) as http:
            client = AsyncElevenLabs(api_key=key, httpx_client=http, timeout=PROVIDER_TIMEOUT_SECONDS)
            await audio.seek(0)
            result = await client.speech_to_text.convert(
                file=(filename, audio.file, audio.content_type),
                model_id="scribe_v2",
                timestamps_granularity="word",
                tag_audio_events=False,
                diarize=False,
                request_options={"max_retries": 0, "timeout_in_seconds": PROVIDER_TIMEOUT_SECONDS},
            )
        stage[0] = "result_mapping"
        words = []
        for word in getattr(result, "words", None) or []:
            if word.type != "word" or word.start is None or word.end is None:
                continue
            words.append(WordTiming(text=word.text, start=word.start, end=word.end))
        mapped = TranscriptionResult(text=result.text, language=getattr(result, "language_code", None), words=words)
        return mapped


def get_transcription_service() -> TranscriptionService:
    # No key or network needed at startup; configuration is checked only on a request.
    return ElevenLabsTranscriptionService()
