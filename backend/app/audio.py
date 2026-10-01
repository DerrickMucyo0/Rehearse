"""Bounded, temporary audio validation; no recordings are retained."""
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.datastructures import UploadFile

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_BODY_BYTES = MAX_AUDIO_BYTES + 64 * 1024
AUDIO_EXTENSIONS = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
}


class AudioAccepted(BaseModel):
    session_id: UUID
    question_index: int
    filename: str
    content_type: str
    size_bytes: int
    status: Literal["accepted"] = "accepted"


async def bounded_multipart_request(request: Request) -> Request:
    """Cap actual received bytes before multipart parsing, even without Content-Length."""
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Use multipart/form-data.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Recording request is too large (10 MiB audio maximum).")
        body.extend(chunk)

    async def receive():
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    return Request(request.scope, receive)


def validate_audio(upload: UploadFile, session_id: UUID, question_index: int) -> AudioAccepted:
    content_type = upload.content_type or ""
    base_type = content_type.split(";", 1)[0].strip().lower()
    if base_type not in AUDIO_EXTENSIONS:
        raise HTTPException(415, "Unsupported audio content type.")
    size = upload.size or 0
    if size == 0:
        raise HTTPException(422, "Recording is empty.")
    if size > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording exceeds the 10 MiB limit.")
    return AudioAccepted(
        session_id=session_id, question_index=question_index,
        filename=f"answer-{question_index + 1}.{AUDIO_EXTENSIONS[base_type]}",
        content_type=content_type, size_bytes=size,
    )


@asynccontextmanager
async def validated_audio(bounded: Request, session_id: UUID):
    """Share multipart shape/audio validation and file lifetime across audio operations."""
    async with bounded.form(max_files=1, max_fields=1, max_part_size=1024) as form:
        upload = form.get("audio")
        index = form.get("question_index")
        if (set(form) != {"audio", "question_index"} or
                not isinstance(upload, UploadFile) or not isinstance(index, str) or
                not index.isascii() or not index.isdecimal() or len(index) > 9):
            raise HTTPException(422, "Provide an audio file and a non-negative question_index.")
        metadata = validate_audio(upload, session_id, int(index))
        yield upload, metadata
