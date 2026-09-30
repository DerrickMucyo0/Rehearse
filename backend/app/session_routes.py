from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from app.sessions import (
    AnswerRequest,
    InterviewSession,
    InterviewSessionService,
    SessionConflict,
    SessionNotFound,
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
def submit_answer(
    session_id: UUID, answer: AnswerRequest, sessions: SessionService
) -> InterviewSession:
    try:
        return sessions.submit_answer(session_id, answer)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
