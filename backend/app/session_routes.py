from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from app.audio import AudioAccepted, bounded_multipart_request, validated_audio
from app.database import create_database_engine, create_session_factory
from app.speaking_metrics import SpeakingMetrics, measure_transcription
from app.sessions import (
    Attempt,
    AttemptRequest,
    AttemptSubmission,
    ContinueRequest,
    InterviewSession,
    InterviewSessionService,
    SessionConflict,
    SessionNotFound,
)

from app.transcription import (
    TranscriptionFailed, TranscriptionResult, TranscriptionService,
    TranscriptionTimeout, TranscriptionUnavailable, get_transcription_service,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@lru_cache(maxsize=1)
def get_session_service() -> InterviewSessionService:
    # Pool the engine, never an ORM Session. Configuration uses DATABASE_URL only
    # and is resolved on first use, preserving database-free imports and health.
    return InterviewSessionService(create_session_factory(create_database_engine()))


SessionService = Annotated[InterviewSessionService, Depends(get_session_service)]


@router.post("", response_model=InterviewSession, status_code=201)
def start_session(response: Response, sessions: SessionService) -> InterviewSession:
    session = sessions.start()
    response.headers["Location"] = f"/api/sessions/{session.id}"
    return session


@router.get("/{session_id}", response_model=InterviewSession)
def get_session(session_id: UUID, sessions: SessionService) -> InterviewSession:
    try:
        return sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{session_id}/questions/{question_index}/attempts", response_model=AttemptSubmission, status_code=201,
)
def submit_attempt(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)],
    answer: AttemptRequest, sessions: SessionService,
) -> AttemptSubmission:
    try:
        return sessions.submit_attempt(session_id, question_index, answer)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{session_id}/questions/{question_index}/continue", response_model=InterviewSession)
def continue_question(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)],
    request: ContinueRequest, sessions: SessionService,
) -> InterviewSession:
    try:
        return sessions.continue_question(session_id, question_index, request)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}/questions/{question_index}/attempts", response_model=list[Attempt])
def get_attempts(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)], sessions: SessionService,
) -> list[Attempt]:
    try:
        return sessions.get_attempts(session_id, question_index)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@asynccontextmanager
async def current_audio(
    session_id: UUID, request: Request, sessions: InterviewSessionService,
    *, require_attempt_revision: bool = False,
):
    try:
        await run_in_threadpool(sessions.get, session_id)
        bounded = await bounded_multipart_request(request)
        async with validated_audio(
            bounded, session_id, require_attempt_revision=require_attempt_revision,
        ) as (upload, metadata, expected):
            await run_in_threadpool(sessions.validate_current_question, session_id, metadata.question_index, expected)
            yield upload, metadata, expected
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{session_id}/audio", response_model=AudioAccepted)
async def accept_audio(session_id: UUID, request: Request, sessions: SessionService) -> AudioAccepted:
    async with current_audio(session_id, request, sessions) as (_, metadata, _):
        return metadata


class SessionTranscription(TranscriptionResult):
    session_id: UUID
    question_index: int
    measurement_id: UUID
    metrics: SpeakingMetrics = Field(
        description="Measurements of the original recognized transcription, not later edited answers. "
                    "Timing estimates exclude leading/trailing recording silence; null means unavailable.",
    )


@router.post("/{session_id}/transcriptions", response_model=SessionTranscription)
async def transcribe_audio(
    session_id: UUID, request: Request, sessions: SessionService,
    transcriber: Annotated[TranscriptionService, Depends(get_transcription_service)],
) -> SessionTranscription:
    async with current_audio(
        session_id, request, sessions, require_attempt_revision=True,
    ) as (upload, metadata, expected):
        try:
            result = await transcriber.transcribe(upload, metadata.filename)
        except TranscriptionUnavailable:
            raise HTTPException(503, "Transcription is not configured on the server.") from None
        except TranscriptionTimeout:
            raise HTTPException(504, "Transcription timed out. Please try again.") from None
        except TranscriptionFailed:
            raise HTTPException(502, "Unable to transcribe this recording. Try again or type your answer.") from None
        metrics = measure_transcription(result.text, result.language, result.words)
        # Revalidate and persist in a separate operation after inference/calculation.
        # Its session lock prevents stale measurement insertion during submission.
        measurement_id = await run_in_threadpool(
            sessions.create_measurement, session_id, metadata.question_index, metrics,
            expected_last_attempt_number=expected,
        )
        return SessionTranscription(
            session_id=session_id, question_index=metadata.question_index, **result.model_dump(),
            metrics=metrics, measurement_id=measurement_id,
        )
