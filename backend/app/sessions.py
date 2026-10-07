from collections.abc import Callable
from dataclasses import dataclass, field
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
from app.roleplay import QuestionEngine, RoleplayContext, RoleplayQuestion, RoleplayTurn
from app.scenarios import SCENARIO_QUESTIONS, ScenarioType, questions_for_scenario
from app.speaking_metrics import SpeakingMetrics

QUESTIONS = SCENARIO_QUESTIONS["job_interview"]


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_type: ScenarioType = "job_interview"


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
    scenario_type: ScenarioType = "job_interview"
    question_engine: QuestionEngine = "deterministic-v1"
    total_questions: Literal[5] = 5
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


@dataclass(frozen=True)
class ContinueAttemptSnapshot:
    question_index: int
    attempt_id: UUID = field(repr=False)
    attempt_number: int
    answer: str = field(repr=False)


@dataclass(frozen=True)
class ContinueSnapshot:
    """Closed-transaction facts; internal identities never enter provider context."""

    session_id: UUID = field(repr=False)
    principal: AuthenticatedPrincipal = field(repr=False)
    question_engine: QuestionEngine
    scenario_type: ScenarioType
    questions: tuple[str, ...] = field(repr=False)
    current_question_index: int
    expected_last_attempt_number: int
    attempts: tuple[ContinueAttemptSnapshot, ...] = field(repr=False)

    @property
    def requires_generation(self) -> bool:
        return self.question_engine == "live-ai-roleplay-v1" and self.current_question_index < 4

    @property
    def context(self) -> RoleplayContext | None:
        if not self.requires_generation:
            return None
        return RoleplayContext(
            scenario_type=self.scenario_type,
            next_question_number=self.current_question_index + 2,
            turns=tuple(RoleplayTurn(
                question_number=attempt.question_index + 1,
                question=self.questions[attempt.question_index], answer=attempt.answer,
            ) for attempt in self.attempts),
        )


_CONTINUE_STATE_CHANGED = "Interview state changed. Recheck before continuing."


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

    def start(self, scenario_type: ScenarioType = "job_interview") -> InterviewSession:
        return self._start(scenario_type, "deterministic-v1")

    def start_adaptive(self, scenario_type: ScenarioType = "job_interview") -> InterviewSession:
        return self._start(scenario_type, "live-ai-roleplay-v1")

    def _start(self, scenario_type: ScenarioType, question_engine: QuestionEngine) -> InterviewSession:
        questions = questions_for_scenario(scenario_type)
        if question_engine == "live-ai-roleplay-v1":
            questions = questions[:1]
        with self._session_factory.begin() as database:
            stored = StoredInterviewSession(
                questions=questions, question_engine=question_engine, scenario_type=scenario_type, user_id=self._principal.user_id,
            )
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
            if stored.question_engine != "deterministic-v1":
                raise SessionConflict("Adaptive sessions require a guarded Continue commit.")
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

    def prepare_continue(
        self, session_id: UUID, question_index: int, request: ContinueRequest,
    ) -> ContinueSnapshot:
        """Read a consistent context, returning no live transaction or ORM object."""
        with self._session_factory.begin() as database:
            # Root state and all candidate attempts share this one SQL snapshot.
            rows = database.execute(
                select(StoredInterviewSession, QuestionAttempt)
                .outerjoin(QuestionAttempt, QuestionAttempt.session_id == StoredInterviewSession.id)
                .where(self._session_predicate(session_id))
                .order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)
            ).all()
            if not rows:
                raise SessionNotFound("Session not found.")
            return self._continue_snapshot(
                rows[0][0], [attempt for _, attempt in rows if attempt is not None],
                question_index, request.expected_last_attempt_number,
            )

    def commit_continue(
        self, snapshot: ContinueSnapshot, next_question: RoleplayQuestion | None,
        *, principal_guard: Callable[[Session], None] | None = None,
    ) -> InterviewSession:
        """Recheck initiating facts and atomically publish one advancement."""
        if type(snapshot) is not ContinueSnapshot:
            raise SessionConflict(_CONTINUE_STATE_CHANGED)
        if snapshot.question_engine == "live-ai-roleplay-v1" and principal_guard is None:
            raise TypeError("An authenticated commit guard is required.")
        if snapshot.requires_generation:
            if type(next_question) is not RoleplayQuestion:
                raise ValueError("A validated next question is required.")
            # Reject non-HTTP callers that bypass the frozen model's validation.
            next_question = RoleplayQuestion.model_validate(next_question.model_dump())
        elif next_question is not None:
            raise ValueError("This Continue must not generate a question.")
        with self._session_factory.begin() as database:
            stored = self._locked_session(database, snapshot.session_id)
            if snapshot.principal != self._principal:
                raise SessionConflict(_CONTINUE_STATE_CHANGED)
            if snapshot.question_engine == "live-ai-roleplay-v1":
                assert principal_guard is not None
                principal_guard(database)
            attempts = list(database.scalars(
                select(QuestionAttempt).where(QuestionAttempt.session_id == stored.id)
                .order_by(QuestionAttempt.question_index, QuestionAttempt.attempt_number)
            ))
            try:
                current = self._continue_snapshot(
                    stored, attempts, snapshot.current_question_index, snapshot.expected_last_attempt_number,
                )
            except SessionConflict:
                raise SessionConflict(_CONTINUE_STATE_CHANGED) from None
            if current != snapshot:
                raise SessionConflict(_CONTINUE_STATE_CHANGED)
            if next_question is not None:
                stored.questions = (*stored.questions, next_question.next_question)
            stored.current_question_index += 1
            if stored.current_question_index == 5:
                stored.status = "completed"
                stored.completed_at = datetime.now(timezone.utc)
            # One flush emits prefix and index together; context manager commits
            # before either a response or finalization becomes visible to callers.
            database.flush()
            return self._read_response(database, stored.id)

    def _continue_snapshot(
        self, stored: StoredInterviewSession, attempts: list[QuestionAttempt],
        question_index: int, expected_last_attempt_number: int,
    ) -> ContinueSnapshot:
        self._check_question(stored, question_index)
        latest: dict[int, QuestionAttempt] = {}
        for attempt in attempts:
            previous = latest.get(attempt.question_index)
            if previous is None or attempt.attempt_number > previous.attempt_number:
                latest[attempt.question_index] = attempt
        target = latest.get(question_index)
        if target is None:
            raise SessionConflict("Current question has no submitted attempts.")
        self._check_revision(target.attempt_number, expected_last_attempt_number)
        if any(index not in latest for index in range(question_index + 1)):
            raise SessionConflict(_CONTINUE_STATE_CHANGED)
        return ContinueSnapshot(
            session_id=stored.id, principal=self._principal,
            question_engine=stored.question_engine, scenario_type=stored.scenario_type, questions=tuple(stored.questions),
            current_question_index=stored.current_question_index,
            expected_last_attempt_number=expected_last_attempt_number,
            attempts=tuple(ContinueAttemptSnapshot(
                question_index=index, attempt_id=latest[index].id,
                attempt_number=latest[index].attempt_number, answer=latest[index].answer_text,
            ) for index in range(question_index + 1)),
        )

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
            id=stored.id, scenario_type=stored.scenario_type, question_engine=stored.question_engine, status=stored.status,
            current_question_index=stored.current_question_index,
            questions=list(stored.questions),
            answers=[latest[index].answer_text for index in range(stored.current_question_index) if index in latest],
            current_question_latest_attempt_number=current.attempt_number if current is not None else 0,
        )
