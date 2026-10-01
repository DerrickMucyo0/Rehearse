from threading import Lock
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field

QUESTIONS = (
    "Tell me about yourself.",
    "Tell me about a challenging problem you solved.",
    "Tell me about a time you worked with a team.",
    "Why are you interested in this opportunity?",
    "What is one project you are proud of and why?",
)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_index: Annotated[int, Field(strict=True, ge=0)]
    answer: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)
    ]


class InterviewSession(BaseModel):
    id: UUID
    status: Literal["active", "completed"] = "active"
    current_question_index: int = 0
    questions: list[str]
    answers: list[str] = Field(default_factory=list)

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

    def start(self) -> InterviewSession:
        session = InterviewSession(id=uuid4(), questions=list(QUESTIONS))
        with self._lock:
            self._sessions[session.id] = session
            return session.model_copy(deep=True)

    def get(self, session_id: UUID) -> InterviewSession:
        with self._lock:
            return self._find(session_id).model_copy(deep=True)

    def submit_answer(self, session_id: UUID, answer: AnswerRequest) -> InterviewSession:
        with self._lock:
            session = self._find(session_id)
            self._check_question(session, answer.question_index)
            session.answers.append(answer.answer)
            session.current_question_index += 1
            if session.current_question_index == len(session.questions):
                session.status = "completed"
            return session.model_copy(deep=True)

    def validate_current_question(self, session_id: UUID, question_index: int) -> None:
        with self._lock:
            self._check_question(self._find(session_id), question_index)

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
