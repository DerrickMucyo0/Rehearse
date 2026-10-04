from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.audio import AudioAccepted, bounded_multipart_request, validated_audio
from app.interview_orchestration import submit_with_reasoning
from app.nemotron import get_reasoning_service
from app.reasoning import InterviewerReasoningService, ReasoningFailed, ReasoningTimeout, ReasoningUnavailable
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
service = InterviewSessionService()


def get_session_service() -> InterviewSessionService:
    return service


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
async def submit_answer(
    session_id: UUID, answer: AnswerRequest, sessions: SessionService,
    reasoner: Annotated[InterviewerReasoningService, Depends(get_reasoning_service)],
) -> InterviewSession:
    try:
        return await submit_with_reasoning(sessions, session_id, answer, reasoner)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReasoningUnavailable:
        raise HTTPException(503, "Interviewer reasoning is not configured on the server.") from None
    except ReasoningTimeout:
        raise HTTPException(504, "Interviewer reasoning timed out. Please retry your answer.") from None
    except ReasoningFailed:
        raise HTTPException(502, "Unable to evaluate this answer. Please retry.") from None


@asynccontextmanager
async def current_audio(session_id: UUID, request: Request, sessions: InterviewSessionService):
    try:
        sessions.get(session_id)
        bounded = await bounded_multipart_request(request)
        async with validated_audio(bounded, session_id) as (upload, metadata):
            sessions.validate_current_turn(session_id, metadata.question_index, metadata.turn_revision)
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
    turn_revision: int


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
        # Reject results for a turn answered in another tab while the provider ran.
        sessions.validate_current_turn(session_id, metadata.question_index, metadata.turn_revision)
        return SessionTranscription(
            session_id=session_id, question_index=metadata.question_index,
            turn_revision=metadata.turn_revision, **result.model_dump(),
        )
