from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from starlette.datastructures import UploadFile

from app.audio import AudioAccepted, bounded_multipart_request, validate_audio
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


@router.post("/{session_id}/audio", response_model=AudioAccepted)
async def accept_audio(session_id: UUID, request: Request, sessions: SessionService) -> AudioAccepted:
    try:
        sessions.get(session_id)
        bounded = await bounded_multipart_request(request)
        # The context closes temporary spooled files on success and validation errors.
        async with bounded.form(max_files=1, max_fields=1, max_part_size=1024) as form:
            upload = form.get("audio")
            index = form.get("question_index")
            if (set(form) != {"audio", "question_index"} or
                    not isinstance(upload, UploadFile) or not isinstance(index, str) or
                    not index.isascii() or not index.isdecimal() or len(index) > 9):
                raise HTTPException(422, "Provide an audio file and a non-negative question_index.")
            question_index = int(index)
            metadata = validate_audio(upload, session_id, question_index)
            # Check after upload parsing so an answer submitted during transfer is rejected.
            sessions.validate_current_question(session_id, question_index)
            return metadata
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
