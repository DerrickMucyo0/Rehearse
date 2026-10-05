from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, field_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from app.database_models import QuestionAttempt, StoredInterviewSession, validate_submitted_answer_text

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

    @field_validator("answer")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        return validate_submitted_answer_text(value)


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
    """PostgreSQL storage; each operation owns and closes its ORM transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(self) -> InterviewSession:
        with self._session_factory.begin() as database:
            stored = StoredInterviewSession(questions=QUESTIONS)
            database.add(stored)
            database.flush()
            return self._response(stored, [])

    def get(self, session_id: UUID) -> InterviewSession:
        with self._session_factory.begin() as database:
            # One statement gives state and ordered answers the same PostgreSQL
            # snapshot, without holding a read lock across separate statements.
            rows = database.execute(
                select(StoredInterviewSession, QuestionAttempt.answer_text)
                .outerjoin(QuestionAttempt, and_(
                    QuestionAttempt.session_id == StoredInterviewSession.id,
                    QuestionAttempt.attempt_number == 1,
                ))
                .where(StoredInterviewSession.id == session_id)
                .order_by(QuestionAttempt.question_index)
            ).all()
            if not rows:
                raise SessionNotFound("Session not found.")
            return self._response(rows[0][0], [answer for _, answer in rows if answer is not None])

    def submit_answer(self, session_id: UUID, answer: AnswerRequest) -> InterviewSession:
        with self._session_factory.begin() as database:
            stored = database.scalar(
                select(StoredInterviewSession)
                .where(StoredInterviewSession.id == session_id)
                .with_for_update()
            )
            if stored is None:
                raise SessionNotFound("Session not found.")
            self._check_question(stored, answer.question_index)
            # Revalidate even for non-HTTP callers that bypass AnswerRequest's
            # validation. NUL is rejected before an INSERT can reach PostgreSQL.
            submitted = validate_submitted_answer_text(answer.answer)
            database.add(QuestionAttempt(
                session_id=stored.id, question_index=answer.question_index,
                attempt_number=1, answer_text=submitted,
            ))
            stored.current_question_index += 1
            if stored.current_question_index == len(stored.questions):
                stored.status = "completed"
                stored.completed_at = datetime.now(timezone.utc)
            database.flush()
            answers = list(database.scalars(
                select(QuestionAttempt.answer_text)
                .where(QuestionAttempt.session_id == stored.id, QuestionAttempt.attempt_number == 1)
                .order_by(QuestionAttempt.question_index)
            ))
            # The context manager commits before the caller receives this DTO.
            # Any exception, including flush/commit failure, rolls everything back.
            return self._response(stored, answers)

    def validate_current_question(self, session_id: UUID, question_index: int) -> None:
        self._check_question(self.get(session_id), question_index)

    @staticmethod
    def _check_question(session: InterviewSession | StoredInterviewSession, question_index: int) -> None:
        if session.status == "completed":
            raise SessionConflict("Session is already completed.")
        if question_index != session.current_question_index:
            raise SessionConflict("Answer does not match the current question.")

    @staticmethod
    def _response(stored: StoredInterviewSession, answers: list[str]) -> InterviewSession:
        return InterviewSession(
            id=stored.id, status=stored.status,
            current_question_index=stored.current_question_index,
            questions=list(stored.questions), answers=answers,
        )
