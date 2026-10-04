"""Coordinate provider advice and an atomic, deterministic session transition."""
import asyncio

from app.reasoning import Decision, InterviewerReasoningService, InvalidDecision, ReasoningTimeout
from app.sessions import AnswerRequest, InterviewSession, InterviewSessionService

REASONING_DEADLINE_SECONDS = 30


async def submit_with_reasoning(
    sessions: InterviewSessionService, session_id, answer: AnswerRequest,
    reasoner: InterviewerReasoningService,
) -> InterviewSession:
    snapshot, replay = sessions.begin_submission(session_id, answer)
    if replay:
        return snapshot
    try:
        decision = None
        if snapshot.probe_count < 2:
            context = sessions.reasoning_context(snapshot, answer)
            try:
                result = await asyncio.wait_for(reasoner.decide(context), REASONING_DEADLINE_SECONDS)
            except TimeoutError:
                raise ReasoningTimeout() from None
            try:
                decision = Decision.model_validate(result)
            except ValueError:
                raise InvalidDecision() from None
        return sessions.submit_answer(session_id, answer, decision)
    finally:
        # Also release a reservation on cancellation; never commit a failed answer.
        sessions.cancel_submission(session_id, answer)
