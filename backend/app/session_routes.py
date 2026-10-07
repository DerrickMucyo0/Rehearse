from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.audio import AudioAccepted, bounded_multipart_request, validated_audio
from app.auth_http import (
    AuthenticatedPrincipalDependency, AuthSessionStoreDependency, revalidate_authenticated_principal,
    revalidate_authenticated_principal_in_transaction,
)
from app.comparisons import AttemptComparison
from app.database import get_database_session_factory
from app.delivery_metrics import DeliveryMetrics, measure_delivery
from app.roleplay import RoleplayAdapter, RoleplayUnavailable
from app.roleplay_application import continue_application_attempt
from app.roleplay_composition import get_roleplay_adapter
from app.semantic_diagnosis import SemanticDiagnosis
from app.semantic_diagnosis_adapter import SemanticDiagnosisAdapter
from app.semantic_diagnosis_application import (
    SemanticDiagnosisFailed,
    SemanticDiagnosisTimeout,
    SemanticDiagnosisUnavailable,
    diagnose_application_context,
)
from app.semantic_diagnosis_composition import get_semantic_diagnosis_adapter
from app.speaking_metrics import SpeakingMetrics, measure_transcription
from app.sessions import (
    Attempt,
    AttemptRequest,
    AttemptSubmission,
    ContinueRequest,
    InvalidComparisonSelection,
    InterviewSession,
    InterviewSessionService,
    SessionConflict,
    SessionNotFound,
    StartSessionRequest,
)

from app.transcription import (
    TranscriptionFailed, TranscriptionResult, TranscriptionService,
    TranscriptionTimeout, TranscriptionUnavailable, get_transcription_service,
)
from app.voice import SpeechFailed, SpeechService, SpeechTimeout, SpeechUnavailable
from app.voice_composition import get_speech_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def get_session_service(principal: AuthenticatedPrincipalDependency) -> InterviewSessionService:
    # Share only the factory. A service/principal belongs to this authenticated
    # request, never a process cache; each operation owns its short transaction.
    return InterviewSessionService(get_database_session_factory(), principal)


SessionService = Annotated[InterviewSessionService, Depends(get_session_service)]

SemanticDiagnosisService = Annotated[
    SemanticDiagnosisAdapter,
    Depends(get_semantic_diagnosis_adapter),
]

RoleplayService = Annotated[RoleplayAdapter, Depends(get_roleplay_adapter)]

QuestionSpeechService = Annotated[SpeechService, Depends(get_speech_service)]


async def _current_speech_question(
    sessions: InterviewSessionService, session_id: UUID, question_index: int,
) -> str:
    try:
        session = await run_in_threadpool(sessions.get, session_id)
    except SessionNotFound:
        raise HTTPException(404, "Session not found.") from None
    if (
        session.status != "active"
        or question_index != session.current_question_index
        or question_index >= len(session.questions)
    ):
        raise HTTPException(404, "Session not found.") from None
    return session.questions[session.current_question_index]


@router.post("/{session_id}/questions/{question_index}/speech", response_class=Response)
async def speak_question(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)], request: Request,
    sessions: SessionService, speaker: QuestionSpeechService,
    principal: AuthenticatedPrincipalDependency, auth_store: AuthSessionStoreDependency,
) -> Response:
    # Reject on the first nonempty chunk, without buffering an arbitrary body.
    # The browser supplies identifiers only; text always comes from owned storage.
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422, "Speech requests do not accept a body.")
    text = await _current_speech_question(sessions, session_id, question_index)
    try:
        speech = await speaker.synthesize(text)
    except SpeechUnavailable:
        raise HTTPException(503, "Voice playback is unavailable right now.") from None
    except SpeechTimeout:
        raise HTTPException(504, "Voice playback is unavailable right now.") from None
    except SpeechFailed:
        raise HTTPException(502, "Voice playback is unavailable right now.") from None
    # Both database operations have completed before provider work. Recheck the
    # initiating login and the current question in fresh, short reads afterwards.
    await run_in_threadpool(revalidate_authenticated_principal, principal, auth_store)
    if await _current_speech_question(sessions, session_id, question_index) != text:
        raise HTTPException(404, "Session not found.") from None
    return Response(speech.audio, media_type="audio/mpeg", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("", response_model=InterviewSession, status_code=201)
def start_session(
    response: Response, sessions: SessionService, body: StartSessionRequest | None = None,
) -> InterviewSession:
    session = sessions.start_adaptive(body.scenario_type if body is not None else "job_interview")
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
async def continue_question(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)],
    request: ContinueRequest, sessions: SessionService,
    roleplayer: RoleplayService,
    principal: AuthenticatedPrincipalDependency,
    auth_store: AuthSessionStoreDependency,
) -> InterviewSession | JSONResponse:
    try:
        return await continue_application_attempt(
            sessions, roleplayer, session_id=session_id, question_index=question_index,
            request=request, principal_guard=lambda database:
                revalidate_authenticated_principal_in_transaction(principal, auth_store, database),
        )
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RoleplayUnavailable:
        return JSONResponse(status_code=503, content={
            "detail": "Interviewer is unavailable right now. Try Continue again.",
            "code": "roleplay_generation_unavailable",
            "write_outcome": "not_applied",
        }, headers={"Cache-Control": "no-store"})


@router.get("/{session_id}/questions/{question_index}/attempts", response_model=list[Attempt])
def get_attempts(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)], sessions: SessionService,
) -> list[Attempt]:
    try:
        return sessions.get_attempts(session_id, question_index)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/{session_id}/questions/{question_index}/attempts/{attempt_number}/diagnosis",
    response_model=SemanticDiagnosis,
)
async def diagnose_attempt(
    session_id: UUID,
    question_index: Annotated[int, Path(ge=0)],
    attempt_number: Annotated[int, Path(ge=1)],
    sessions: SessionService,
    diagnoser: SemanticDiagnosisService,
    principal: AuthenticatedPrincipalDependency,
    auth_store: AuthSessionStoreDependency,
) -> SemanticDiagnosis:
    try:
        context = await run_in_threadpool(
            sessions.get_diagnosis_context,
            session_id,
            question_index,
            attempt_number,
        )
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        _, diagnosis = await diagnose_application_context(diagnoser, context)
    except SemanticDiagnosisUnavailable:
        raise HTTPException(status_code=503, detail="Semantic diagnosis is not configured.") from None
    except SemanticDiagnosisTimeout:
        raise HTTPException(status_code=504, detail="Semantic diagnosis timed out.") from None
    except SemanticDiagnosisFailed:
        raise HTTPException(status_code=502, detail="Unable to generate semantic diagnosis.") from None
    await run_in_threadpool(revalidate_authenticated_principal, principal, auth_store)
    try:
        # A new owned read rechecks the exact persisted target after inference.
        # Historical attempts remain diagnosable even if the question advanced.
        current_context = await run_in_threadpool(
            sessions.get_diagnosis_context, session_id, question_index, attempt_number,
        )
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if current_context != context:
        raise HTTPException(status_code=409, detail="Attempt context no longer matches the diagnosis request.")
    return diagnosis


@router.get("/{session_id}/questions/{question_index}/comparison", response_model=AttemptComparison)
def get_comparison(
    session_id: UUID, question_index: Annotated[int, Path(ge=0)], sessions: SessionService,
    before: Annotated[int | None, Query(ge=1)] = None,
    after: Annotated[int | None, Query(ge=1)] = None,
) -> AttemptComparison:
    try:
        return sessions.get_comparison(session_id, question_index, before=before, after=after)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidComparisonSelection as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


class SessionDeliveryMetrics(DeliveryMetrics):
    source: Literal["original_transcription"]


class SessionTranscription(TranscriptionResult):
    session_id: UUID
    question_index: int
    measurement_id: UUID
    metrics: SpeakingMetrics = Field(
        description="Measurements of the original recognized transcription, not later edited answers. "
                    "Timing estimates exclude leading/trailing recording silence; null means unavailable.",
    )
    delivery_metrics: SessionDeliveryMetrics


@router.post("/{session_id}/transcriptions", response_model=SessionTranscription)
async def transcribe_audio(
    session_id: UUID, request: Request, sessions: SessionService,
    transcriber: Annotated[TranscriptionService, Depends(get_transcription_service)],
    principal: AuthenticatedPrincipalDependency,
    auth_store: AuthSessionStoreDependency,
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
        await run_in_threadpool(revalidate_authenticated_principal, principal, auth_store)
        metrics = measure_transcription(result.text, result.language, result.words)
        delivery_metrics = measure_delivery(result.text, result.words)
        # Revalidate and persist in a separate operation after inference/calculation.
        # Its session lock prevents stale measurement insertion during submission.
        measurement_id = await run_in_threadpool(
            sessions.create_measurement, session_id, metadata.question_index, metrics,
            expected_last_attempt_number=expected, delivery_metrics=delivery_metrics,
        )
        return SessionTranscription(
            session_id=session_id, question_index=metadata.question_index, **result.model_dump(),
            metrics=metrics, measurement_id=measurement_id,
            delivery_metrics=SessionDeliveryMetrics(source=metrics.source, **delivery_metrics.model_dump()),
        )
