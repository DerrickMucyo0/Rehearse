from threading import Lock
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field

from app.reasoning import Action, Decision, PriorTurn, ReasoningContext

QUESTIONS = (
    "Tell me about yourself.",
    "Tell me about a challenging problem you solved.",
    "Tell me about a time you worked with a team.",
    "Why are you interested in this opportunity?",
    "What is one project you are proud of and why?",
)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_index: Annotated[int, Field(strict=True, ge=0)]
    turn_revision: Annotated[int, Field(strict=True, ge=0)]
    submission_id: UUID
    answer: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)
    ]


class TurnRecord(BaseModel):
    question_index: int
    turn_revision: int
    submission_id: UUID
    prompt: str
    answer: str
    action: Action | None
    transition_source: Literal["nemotron", "probe_limit"]


class InterviewSession(BaseModel):
    id: UUID
    status: Literal["active", "completed"] = "active"
    current_question_index: int = 0
    questions: list[str]
    # Original answer for each planned question; follow-ups live in turns.
    answers: list[str] = Field(default_factory=list)
    turn_revision: int = 0
    current_prompt: str | None = None
    probe_count: int = 0
    turns: list[TurnRecord] = Field(default_factory=list)

    @computed_field
    @property
    def current_question(self) -> str | None:
        if self.status == "completed":
            return None
        return self.questions[self.current_question_index]


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


class InterviewSessionService:
    """Process-local storage and transitions; routes only depend on this service."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, InterviewSession] = {}
        self._lock = Lock()
        self._pending: dict[UUID, AnswerRequest] = {}

    def start(self) -> InterviewSession:
        session = InterviewSession(id=uuid4(), questions=list(QUESTIONS), current_prompt=QUESTIONS[0])
        with self._lock:
            self._sessions[session.id] = session
            return session.model_copy(deep=True)

    def get(self, session_id: UUID) -> InterviewSession:
        with self._lock:
            return self._find(session_id).model_copy(deep=True)

    def begin_submission(self, session_id: UUID, answer: AnswerRequest) -> tuple[InterviewSession, bool]:
        """Reserve one turn. A committed retry returns current authoritative state."""
        with self._lock:
            session = self._find(session_id)
            for turn in session.turns:
                if turn.submission_id == answer.submission_id:
                    if (turn.question_index, turn.turn_revision, turn.answer) != (
                        answer.question_index, answer.turn_revision, answer.answer
                    ):
                        raise SessionConflict("Submission identifier was already used.")
                    return session.model_copy(deep=True), True
            self._check_turn(session, answer.question_index, answer.turn_revision)
            if session_id in self._pending:
                raise SessionConflict("An answer is already being processed. Retry shortly.")
            self._pending[session_id] = answer
            return session.model_copy(deep=True), False

    def cancel_submission(self, session_id: UUID, answer: AnswerRequest) -> None:
        with self._lock:
            if self._pending.get(session_id) == answer:
                del self._pending[session_id]

    @staticmethod
    def reasoning_context(session: InterviewSession, answer: AnswerRequest) -> ReasoningContext:
        return ReasoningContext(
            question=session.questions[session.current_question_index],
            current_prompt=session.current_prompt,
            prior_turns=tuple(PriorTurn(prompt=t.prompt, answer=t.answer) for t in session.turns
                              if t.question_index == session.current_question_index),
            answer=answer.answer,
        )

    def submit_answer(self, session_id: UUID, answer: AnswerRequest, decision: Decision | None) -> InterviewSession:
        """Only this locked engine operation commits answers and transitions."""
        # Revalidate even application/provider instances constructed without validation.
        if decision is not None:
            decision = Decision.model_validate(decision)
        with self._lock:
            session = self._find(session_id)
            self._check_turn(session, answer.question_index, answer.turn_revision)
            if self._pending.get(session_id) != answer:
                raise SessionConflict("Submission is no longer pending.")
            capped = session.probe_count >= 2
            if not capped and decision is None:
                raise ValueError("A validated decision is required")
            advance = capped or decision.action == "MOVE_ON"
            if session.probe_count == 0:
                session.answers.append(answer.answer)
            session.turns.append(TurnRecord(
                question_index=answer.question_index, turn_revision=answer.turn_revision,
                submission_id=answer.submission_id, prompt=session.current_prompt, answer=answer.answer,
                action=None if capped else decision.action,
                transition_source="probe_limit" if capped else "nemotron",
            ))
            session.turn_revision += 1
            if advance:
                session.current_question_index += 1
                session.probe_count = 0
                if session.current_question_index == len(session.questions):
                    session.status = "completed"
                session.current_prompt = session.current_question
            else:
                session.probe_count += 1
                session.current_prompt = decision.next_prompt
            del self._pending[session_id]
            return session.model_copy(deep=True)

    def validate_current_turn(self, session_id: UUID, question_index: int, turn_revision: int) -> None:
        with self._lock:
            self._check_turn(self._find(session_id), question_index, turn_revision)

    @classmethod
    def _check_turn(cls, session: InterviewSession, question_index: int, turn_revision: int) -> None:
        cls._check_question(session, question_index)
        if turn_revision != session.turn_revision:
            raise SessionConflict("Answer does not match the current turn.")

    @staticmethod
    def _check_question(session: InterviewSession, question_index: int) -> None:
        if session.status == "completed":
            raise SessionConflict("Session is already completed.")
        if question_index != session.current_question_index:
            raise SessionConflict("Answer does not match the current question.")

    def _find(self, session_id: UUID) -> InterviewSession:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFound("Session not found.") from None
