from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from app.audio import AudioAccepted, bounded_multipart_request, validated_audio
from app.database import create_database_engine, create_session_factory
from app.speaking_metrics import SpeakingMetrics, measure_transcription
from app.sessions import (
    AnswerRequest,
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


@router.post("/{session_id}/answers", response_model=InterviewSession)
def submit_answer(
    session_id: UUID, answer: AnswerRequest, sessions: SessionService
) -> InterviewSession:
    try:
        return sessions.submit_answer(session_id, answer)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@asynccontextmanager
async def current_audio(session_id: UUID, request: Request, sessions: InterviewSessionService):
    try:
        await run_in_threadpool(sessions.get, session_id)
        bounded = await bounded_multipart_request(request)
        async with validated_audio(bounded, session_id) as (upload, metadata):
            await run_in_threadpool(sessions.validate_current_question, session_id, metadata.question_index)
            yield upload, metadata
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{session_id}/audio", response_model=AudioAccepted)
async def accept_audio(session_id: UUID, request: Request, sessions: SessionService) -> AudioAccepted:
    async with current_audio(session_id, request, sessions) as (_, metadata):
        return metadata


class SessionTranscription(TranscriptionResult):
    session_id: UUID
    question_index: int
    metrics: SpeakingMetrics = Field(
        description="Measurements of the original recognized transcription, not later edited answers. "
                    "Timing estimates exclude leading/trailing recording silence; null means unavailable.",
    )


@router.post("/{session_id}/transcriptions", response_model=SessionTranscription)
async def transcribe_audio(
    session_id: UUID, request: Request, sessions: SessionService,
    transcriber: Annotated[TranscriptionService, Depends(get_transcription_service)],
) -> SessionTranscription:
    async with current_audio(session_id, request, sessions) as (upload, metadata):
        try:
            result = await transcriber.transcribe(upload, metadata.filename)
        except TranscriptionUnavailable:
            raise HTTPException(503, "Transcription is not configured on the server.") from None
        except TranscriptionTimeout:
            raise HTTPException(504, "Transcription timed out. Please try again.") from None
        except TranscriptionFailed:
            raise HTTPException(502, "Unable to transcribe this recording. Try again or type your answer.") from None
        # Reject results for a question answered in another tab while the provider ran.
        await run_in_threadpool(sessions.validate_current_question, session_id, metadata.question_index)
        return SessionTranscription(
            session_id=session_id, question_index=metadata.question_index, **result.model_dump(),
            metrics=measure_transcription(result.text, result.language, result.words),
        )
