from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, field_validator
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.comparisons import (
    AttemptComparison, ComparedAttempt, MeasurementSnapshot, compare_delivery_measurements,
    compare_measurements, delivery_snapshot,
)
from app.database_models import (
    MEASUREMENT_VERSION, QuestionAttempt, StoredInterviewSession,
    TranscriptionMeasurement, validate_submitted_answer_text,
)
from app.delivery_metrics import DeliveryMetrics
from app.diagnosis import DiagnosisContext, build_diagnosis_context
from app.speaking_metrics import SpeakingMetrics

QUESTIONS = (
    "Tell me about yourself.",
    "Tell me about a challenging problem you solved.",
    "Tell me about a time you worked with a team.",
    "Why are you interested in this opportunity?",
    "What is one project you are proud of and why?",
)


class AttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_last_attempt_number: Annotated[int, Field(strict=True, ge=0)]
    answer: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=10000)
    ]
    measurement_id: UUID | None = None

    @field_validator("answer")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        return validate_submitted_answer_text(value)


class ContinueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_last_attempt_number: Annotated[int, Field(strict=True, ge=0)]


class Attempt(BaseModel):
    id: UUID
    question_index: int
    attempt_number: int
    answer: str
    submitted_at: datetime
    measurement_id: UUID | None


class InterviewSession(BaseModel):
    id: UUID
    status: Literal["active", "completed"] = "active"
    current_question_index: int = 0
    questions: list[str]
    answers: list[str] = Field(default_factory=list)
    current_question_latest_attempt_number: int = 0

    @computed_field
    @property
    def current_question(self) -> str | None:
        if self.status == "completed":
            return None
        return self.questions[self.current_question_index]


class AttemptSubmission(BaseModel):
    attempt: Attempt
    session: InterviewSession


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


class InvalidComparisonSelection(Exception):
    pass


class InterviewSessionService:
    """One authenticated owner; every operation closes its own transaction.

    Authentication belongs to the HTTP/store boundary. This service authorizes
    by filtering the root session in the same SELECT, including row locks, before
    interpreting child rows, question state, revisions or measurement references.
    The shared factory carries no identity; this service is never globally cached.
    """

    def __init__(
        self, session_factory: sessionmaker[Session], principal: AuthenticatedPrincipal,
    ) -> None:
        if type(principal) is not AuthenticatedPrincipal:
            raise TypeError("An authenticated principal is required.")
        self._session_factory = session_factory
        self._principal = principal

    def _session_predicate(self, session_id: UUID):
        return and_(
            StoredInterviewSession.id == session_id,
            StoredInterviewSession.user_id == self._principal.user_id,
        )

    def start(self) -> InterviewSession:
        with self._session_factory.begin() as database:
            stored = StoredInterviewSession(questions=QUESTIONS, user_id=self._principal.user_id)
            database.add(stored)
            database.flush()
            return self._response(stored, [])

    def get(self, session_id: UUID) -> InterviewSession:
        with self._session_factory.begin() as database:
            return self._read_response(database, session_id)

    def get_attempts(self, session_id: UUID, question_index: int) -> list[Attempt]:
        with self._session_factory.begin() as database:
            # Existence, immutable snapshot and attempts share one read snapshot.
            rows = database.execute(
                select(StoredInterviewSession, QuestionAttempt)
                .outerjoin(QuestionAttempt, and_(
                    QuestionAttempt.session_id == StoredInterviewSession.id,
                    QuestionAttempt.question_index == question_index,
                ))
                .where(self._session_predicate(session_id))
                .order_by(QuestionAttempt.attempt_number)
            ).all()
            if not rows:
                raise SessionNotFound("Session not found.")
            if not 0 <= question_index < len(rows[0][0].questions):
                raise SessionNotFound("Question not found.")
            return [self._attempt_response(attempt) for _, attempt in rows if attempt is not None]

    def get_comparison(
        self, session_id: UUID, question_index: int,
        before: int | None = None, after: int | None = None,
    ) -> AttemptComparison:
        with self._session_factory.begin() as database:
            _, attempts = self._read_question_attempts(database, session_id, question_index)
            if before is None and after is None and len(attempts) < 2:
                baseline = attempts.get(1)
                return AttemptComparison(
                    session_id=session_id, question_index=question_index,
                    before_attempt=self._compared_attempt(*baseline) if baseline is not None else None,
                    after_attempt=None, comparison=None,
                )
            before_number = 1 if before is None else before
            after_number = max(attempts, default=0) if after is None else after
            if before_number not in attempts or after_number not in attempts:
                raise SessionNotFound("Attempt not found.")
            if before_number >= after_number:
                raise InvalidComparisonSelection("before must be less than after.")
            before_attempt, before_measurement = attempts[before_number]
            after_attempt, after_measurement = attempts[after_number]
            before_snapshot = self._measurement_snapshot(before_measurement)
            after_snapshot = self._measurement_snapshot(after_measurement)
            return AttemptComparison(
                session_id=session_id, question_index=question_index,
                before_attempt=self._compared_attempt(before_attempt, before_measurement),
                after_attempt=self._compared_attempt(after_attempt, after_measurement),
                comparison=compare_measurements(before_snapshot, after_snapshot),
                delivery_comparison=compare_delivery_measurements(before_snapshot, after_snapshot),
            )

    def get_diagnosis_context(
        self, session_id: UUID, question_index: int, attempt_number: int,
    ) -> DiagnosisContext:
        """Project an exact persisted attempt and only its immediate predecessor."""
        with self._session_factory.begin() as database:
            stored, attempts = self._read_question_attempts(database, session_id, question_index)
            if attempt_number not in attempts:
                raise SessionNotFound("Attempt not found.")
            target, target_measurement = attempts[attempt_number]
            target_snapshot = self._measurement_snapshot(target_measurement)
            predecessor = attempts.get(target.attempt_number - 1)
            comparison = None
            if predecessor is not None:
                before_attempt, before_measurement = predecessor
                before_snapshot = self._measurement_snapshot(before_measurement)
                comparison = AttemptComparison(
                    session_id=session_id, question_index=target.question_index,
                    before_attempt=self._compared_attempt(before_attempt, before_measurement),
                    after_attempt=self._compared_attempt(target, target_measurement),
                    comparison=compare_measurements(before_snapshot, target_snapshot),
                    delivery_comparison=compare_delivery_measurements(before_snapshot, target_snapshot),
                )
            return build_diagnosis_context(
                question=stored.questions[target.question_index], answer=target.answer_text,
                question_index=target.question_index, attempt_number=target.attempt_number,
                measurement=target_snapshot, comparison=comparison,
            )

    def submit_attempt(
        self, session_id: UUID, question_index: int, answer: AttemptRequest,
    ) -> AttemptSubmission:
        with self._session_factory.begin() as database:
            stored = self._locked_session(database, session_id)
            self._check_question(stored, question_index)
            latest = self._latest_attempt_number(database, session_id, question_index)
            self._check_revision(latest, answer.expected_last_attempt_number)
            # Revalidate even for non-HTTP callers that bypass AttemptRequest's
            # validation. NUL is rejected before an INSERT can reach PostgreSQL.
            submitted = validate_submitted_answer_text(answer.answer)
            if answer.measurement_id is not None:
                # Lock only an exact measurement in the owning context. Unknown
                # IDs and context mismatches have the same public error.
                measurement = database.scalar(
                    select(TranscriptionMeasurement)
                    .where(
                        TranscriptionMeasurement.id == answer.measurement_id,
                        TranscriptionMeasurement.session_id == stored.id,
                        TranscriptionMeasurement.question_index == stored.current_question_index,
                    )
                    .with_for_update()
                )
                if measurement is None or database.scalar(
                    select(QuestionAttempt.id)
                    .where(QuestionAttempt.measurement_id == answer.measurement_id)
                ) is not None:
                    raise SessionConflict("Measurement cannot be attached to this answer.")
            attempt = QuestionAttempt(
                session_id=stored.id, question_index=question_index,
                attempt_number=latest + 1, answer_text=submitted, measurement_id=answer.measurement_id,
            )
            database.add(attempt)
            database.flush()
            # Submit only appends. The context commits before returning either DTO.
            return AttemptSubmission(
                attempt=self._attempt_response(attempt),
                session=self._read_response(database, session_id),
            )

    def continue_question(
        self, session_id: UUID, question_index: int, request: ContinueRequest,
    ) -> InterviewSession:
        with self._session_factory.begin() as database:
            stored = self._locked_session(database, session_id)
            self._check_question(stored, question_index)
            latest = self._latest_attempt_number(database, session_id, question_index)
            if latest == 0:
                raise SessionConflict("Current question has no submitted attempts.")
            self._check_revision(latest, request.expected_last_attempt_number)
            stored.current_question_index += 1
            if stored.current_question_index == len(stored.questions):
                stored.status = "completed"
                stored.completed_at = datetime.now(timezone.utc)
            database.flush()
            return self._read_response(database, session_id)

    def create_measurement(
        self, session_id: UUID, question_index: int, metrics: SpeakingMetrics,
        *, expected_last_attempt_number: int, delivery_metrics: DeliveryMetrics | None = None,
    ) -> UUID:
        """Persist only original metrics after inference, in a new transaction.

        The session lock closes the revalidation/insert race with answer submission.
        No provider call, audio, transcript text or word timing array enters here.
        Legacy callers may omit delivery facts; transcription always supplies the
        calculated family, including explicit unavailability, before this insert.
        """
        delivery_fields = {} if delivery_metrics is None else {
            "delivery_measurement_version": delivery_metrics.version,
            "pause_count": delivery_metrics.pause_count,
            "total_pause_duration_seconds": delivery_metrics.total_pause_duration_seconds,
            "longest_pause_seconds": delivery_metrics.longest_pause_seconds,
            "pause_unavailable_reason": delivery_metrics.unavailable_reason,
        }
        with self._session_factory.begin() as database:
            stored = self._locked_session(database, session_id)
            self._check_question(stored, question_index)
            self._check_revision(
                self._latest_attempt_number(database, session_id, question_index),
                expected_last_attempt_number,
            )
            measurement = TranscriptionMeasurement(
                session_id=stored.id, question_index=stored.current_question_index,
                created_at=datetime.now(timezone.utc), measurement_version=MEASUREMENT_VERSION,
                measurement_source=metrics.source, recognized_word_count=metrics.recognized_word_count,
                um_count=metrics.um_count, uh_count=metrics.uh_count,
                filler_unavailable_reason=metrics.filler_unavailable_reason,
                timed_utterance_span_seconds=metrics.timed_utterance_span_seconds,
                estimated_words_per_minute=metrics.estimated_words_per_minute,
                timing_unavailable_reason=metrics.timing_unavailable_reason,
                **delivery_fields,
            )
            database.add(measurement)
            database.flush()
            return measurement.id

    def validate_current_question(
        self, session_id: UUID, question_index: int, expected_last_attempt_number: int | None = None,
    ) -> None:
        session = self.get(session_id)
        self._check_question(session, question_index)
        if expected_last_attempt_number is not None:
            self._check_revision(session.current_question_latest_attempt_number, expected_last_attempt_number)

    def _locked_session(self, database: Session, session_id: UUID) -> StoredInterviewSession:
        stored = database.scalar(
            select(StoredInterviewSession).where(self._session_predicate(session_id)).with_for_update()
        )
        if stored is None:
            raise SessionNotFound("Session not found.")
        return stored

    @staticmethod
    def _latest_attempt_number(database: Session, session_id: UUID, question_index: int) -> int:
        return database.scalar(select(func.coalesce(func.max(QuestionAttempt.attempt_number), 0)).where(
            QuestionAttempt.session_id == session_id, QuestionAttempt.question_index == question_index,
        ))

    @staticmethod
    def _check_revision(latest: int, expected: int) -> None:
        if expected != latest:
            raise SessionConflict("Attempt revision does not match the current question.")

    def _read_question_attempts(
        self, database: Session, session_id: UUID, question_index: int,
    ) -> tuple[StoredInterviewSession, dict[int, tuple[QuestionAttempt, TranscriptionMeasurement | None]]]:
        # One statement selects the immutable question snapshot, scoped attempts
        # and their exact linked measurements. No read lock or latest-measurement
        # inference is needed, even if another retry commits during this read.
        rows = database.execute(
            select(StoredInterviewSession, QuestionAttempt, TranscriptionMeasurement)
            .select_from(StoredInterviewSession)
            .outerjoin(QuestionAttempt, and_(
                QuestionAttempt.session_id == StoredInterviewSession.id,
                QuestionAttempt.question_index == question_index,
            ))
            .outerjoin(TranscriptionMeasurement, and_(
                TranscriptionMeasurement.id == QuestionAttempt.measurement_id,
                TranscriptionMeasurement.session_id == QuestionAttempt.session_id,
                TranscriptionMeasurement.question_index == QuestionAttempt.question_index,
            ))
            .where(self._session_predicate(session_id))
            .order_by(QuestionAttempt.attempt_number)
        ).all()
        if not rows:
            raise SessionNotFound("Session not found.")
        stored = rows[0][0]
        if not 0 <= question_index < len(stored.questions):
            raise SessionNotFound("Question not found.")
        return stored, {
            attempt.attempt_number: (attempt, measurement)
            for _, attempt, measurement in rows if attempt is not None
        }

    def _read_response(self, database: Session, session_id: UUID) -> InterviewSession:
        # State, finalized answers and the current revision share one SQL snapshot.
        rows = database.execute(
            select(StoredInterviewSession, QuestionAttempt)
            .outerjoin(QuestionAttempt, QuestionAttempt.session_id == StoredInterviewSession.id)
            .where(self._session_predicate(session_id))
            .order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)
        ).all()
        if not rows:
            raise SessionNotFound("Session not found.")
        return self._response(rows[0][0], [attempt for _, attempt in rows if attempt is not None])

    @staticmethod
    def _attempt_response(attempt: QuestionAttempt) -> Attempt:
        return Attempt(
            id=attempt.id, question_index=attempt.question_index, attempt_number=attempt.attempt_number,
            answer=attempt.answer_text, submitted_at=attempt.submitted_at, measurement_id=attempt.measurement_id,
        )

    @staticmethod
    def _compared_attempt(
        attempt: QuestionAttempt, measurement: TranscriptionMeasurement | None,
    ) -> ComparedAttempt:
        return ComparedAttempt(
            id=attempt.id, attempt_number=attempt.attempt_number, measurement_id=attempt.measurement_id,
            measurement_version=measurement.measurement_version if measurement is not None else None,
            measurement_source=measurement.measurement_source if measurement is not None else None,
        )

    @staticmethod
    def _measurement_snapshot(measurement: TranscriptionMeasurement | None) -> MeasurementSnapshot | None:
        if measurement is None:
            return None
        return MeasurementSnapshot(
            measurement_version=measurement.measurement_version,
            measurement_source=measurement.measurement_source,
            recognized_word_count=measurement.recognized_word_count,
            um_count=measurement.um_count, uh_count=measurement.uh_count,
            filler_unavailable_reason=measurement.filler_unavailable_reason,
            timed_utterance_span_seconds=measurement.timed_utterance_span_seconds,
            estimated_words_per_minute=measurement.estimated_words_per_minute,
            timing_unavailable_reason=measurement.timing_unavailable_reason,
            delivery_metrics=delivery_snapshot(
                measurement.delivery_measurement_version, measurement.measurement_source,
                measurement.pause_count, measurement.total_pause_duration_seconds,
                measurement.longest_pause_seconds, measurement.pause_unavailable_reason,
            ),
        )

    @staticmethod
    def _check_question(session: InterviewSession | StoredInterviewSession, question_index: int) -> None:
        if session.status == "completed":
            raise SessionConflict("Session is already completed.")
        if question_index != session.current_question_index:
            raise SessionConflict("Answer does not match the current question.")

    @staticmethod
    def _response(stored: StoredInterviewSession, attempts: list[QuestionAttempt]) -> InterviewSession:
        latest: dict[int, QuestionAttempt] = {}
        for attempt in attempts:
            previous = latest.get(attempt.question_index)
            if previous is None or attempt.attempt_number > previous.attempt_number:
                latest[attempt.question_index] = attempt
        current = latest.get(stored.current_question_index) if stored.status == "active" else None
        return InterviewSession(
            id=stored.id, status=stored.status,
            current_question_index=stored.current_question_index,
            questions=list(stored.questions),
            answers=[latest[index].answer_text for index in range(stored.current_question_index) if index in latest],
            current_question_latest_attempt_number=current.attempt_number if current is not None else 0,
        )
