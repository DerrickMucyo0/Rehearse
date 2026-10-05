"""Capability-scoped, read-only projections of persisted practice facts.

These reads select exact attempt-linked measurements, never infer or recalculate
them. No answer text is loaded for summaries or question overviews.
"""
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator,
)
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.comparisons import MeasurementSnapshot, delivery_snapshot
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.sessions import SessionNotFound

Nonnegative = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, gt=0)]


class HistoryIntegrityError(Exception):
    """Stored facts violate the supported lifecycle; never expose their values."""


class HistoryBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ids: Annotated[list[UUID], Field(min_length=1, max_length=50)]

    @field_validator("session_ids")
    @classmethod
    def canonical_unique_ids(cls, values: list[UUID]) -> list[UUID]:
        # Runs after raw-list length validation: duplicates cannot evade the cap.
        return list(dict.fromkeys(values))


class HistoryDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_index: Nonnegative | None = None
    after_attempt_number: Positive | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10

    @model_validator(mode="after")
    def cursor_requires_question(self):
        if self.after_attempt_number is not None and self.question_index is None:
            raise ValueError("An attempt cursor requires a question index.")
        return self


class HistoryMeasurement(MeasurementSnapshot):
    """Explicit persisted scalar facts; no measurement UUID or content."""

    measurement_source: Literal["original_transcription"]

    @model_validator(mode="after")
    def consistent_availability(self):
        fillers_available = self.filler_unavailable_reason is None
        if ((self.um_count is not None) != fillers_available or
                (self.uh_count is not None) != fillers_available):
            raise ValueError("Inconsistent stored filler availability.")
        timing_available = self.timing_unavailable_reason is None
        if ((self.timed_utterance_span_seconds is not None) != timing_available or
                (self.estimated_words_per_minute is not None) != timing_available):
            raise ValueError("Inconsistent stored timing availability.")
        return self


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FinalizedPoint(ReadModel):
    question_index: Nonnegative
    attempt_id: UUID
    attempt_number: Positive
    submitted_at: AwareDatetime
    measurement: HistoryMeasurement | None


class SessionSummary(ReadModel):
    session_id: UUID
    status: Literal["active", "completed"]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    current_question_number: Positive | None
    total_questions: Positive
    finalized_question_count: Nonnegative
    questions_practiced_count: Nonnegative
    total_attempt_count: Nonnegative
    total_retry_count: Nonnegative
    measured_final_answer_count: Nonnegative
    last_submitted_at: AwareDatetime | None
    last_saved_activity_at: AwareDatetime
    finalized_points: list[FinalizedPoint]


class HistorySummaries(ReadModel):
    summaries: list[SessionSummary]
    missing_session_ids: list[UUID]


class QuestionOverview(ReadModel):
    question_index: Nonnegative
    question_text: str
    finalized: bool
    attempt_count: Nonnegative
    latest_attempt_id: UUID | None
    latest_attempt_number: Positive | None
    final_attempt_id: UUID | None
    final_attempt_number: Positive | None


class HistoryAttempt(ReadModel):
    attempt_id: UUID
    attempt_number: Positive
    answer_text: str
    submitted_at: AwareDatetime
    is_final: bool
    measurement: HistoryMeasurement | None


class SelectedQuestion(ReadModel):
    question_index: Nonnegative
    attempts: list[HistoryAttempt]
    has_more: bool
    next_after_attempt_number: Positive | None


class HistoryDetail(ReadModel):
    summary: SessionSummary
    questions: list[QuestionOverview]
    selected_question: SelectedQuestion | None


@dataclass(frozen=True)
class _SessionRead:
    summary: SessionSummary
    questions: list[QuestionOverview]


_MEASUREMENT_FIELDS = (
    "measurement_version", "measurement_source", "recognized_word_count", "um_count", "uh_count",
    "filler_unavailable_reason", "timed_utterance_span_seconds", "estimated_words_per_minute",
    "timing_unavailable_reason",
)
_DELIVERY_FIELDS = (
    "delivery_measurement_version", "pause_count", "total_pause_duration_seconds",
    "longest_pause_seconds", "pause_unavailable_reason",
)


def _measurement_columns():
    # The internal IDs detect missing/mismatched links; DTOs never serialize them.
    return (
        TranscriptionMeasurement.id.label("linked_measurement_id"),
        *(getattr(TranscriptionMeasurement, name) for name in _MEASUREMENT_FIELDS),
        *(getattr(TranscriptionMeasurement, name) for name in _DELIVERY_FIELDS),
    )


def _measurement_join():
    return and_(
        TranscriptionMeasurement.id == QuestionAttempt.measurement_id,
        TranscriptionMeasurement.session_id == QuestionAttempt.session_id,
        TranscriptionMeasurement.question_index == QuestionAttempt.question_index,
    )


def _measurement(row) -> HistoryMeasurement | None:
    if row["measurement_id"] is None:
        if row["linked_measurement_id"] is not None:
            raise HistoryIntegrityError()
        return None
    if row["linked_measurement_id"] != row["measurement_id"]:
        raise HistoryIntegrityError()
    try:
        delivery = delivery_snapshot(
            row["delivery_measurement_version"], row["measurement_source"], row["pause_count"],
            row["total_pause_duration_seconds"], row["longest_pause_seconds"],
            row["pause_unavailable_reason"],
        )
    except ValueError:
        raise HistoryIntegrityError() from None
    return HistoryMeasurement(
        **{name: row[name] for name in _MEASUREMENT_FIELDS},
        delivery_metrics=delivery,
    )


class HistoryReadService:
    """One bounded batch projection; fixed queries for a selected detail page."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _read_only(database: Session) -> None:
        # Detail has two SELECTs. READ COMMITTED would permit different snapshots.
        # This applies only to this operation, never the pooled connection default.
        database.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

    def get_summaries(self, session_ids: list[UUID]) -> HistorySummaries:
        identifiers = HistoryBatchRequest(session_ids=session_ids).session_ids
        with self._session_factory() as database:
            self._read_only(database)
            reads = self._read_sessions(database, identifiers)
        summaries = sorted(reads.values(), key=lambda item: item.summary.session_id.int)
        summaries.sort(key=lambda item: item.summary.last_saved_activity_at, reverse=True)
        return HistorySummaries(
            summaries=[item.summary for item in summaries],
            missing_session_ids=[identifier for identifier in identifiers if identifier not in reads],
        )

    def get_detail(
        self, session_id: UUID, question_index: int | None = None,
        after_attempt_number: int | None = None, limit: int = 10,
    ) -> HistoryDetail:
        query = HistoryDetailQuery(
            question_index=question_index, after_attempt_number=after_attempt_number, limit=limit,
        )
        with self._session_factory() as database:
            self._read_only(database)
            read = self._read_sessions(database, [session_id]).get(session_id)
            if read is None:
                raise SessionNotFound()
            page = None
            if query.question_index is not None:
                if query.question_index >= read.summary.total_questions:
                    raise SessionNotFound()
                overview = read.questions[query.question_index]
                rows = database.execute(
                    select(
                        QuestionAttempt.id.label("attempt_id"), QuestionAttempt.attempt_number,
                        QuestionAttempt.answer_text, QuestionAttempt.submitted_at,
                        QuestionAttempt.measurement_id, *_measurement_columns(),
                    )
                    .select_from(QuestionAttempt)
                    .outerjoin(TranscriptionMeasurement, _measurement_join())
                    .where(
                        QuestionAttempt.session_id == session_id,
                        QuestionAttempt.question_index == query.question_index,
                        QuestionAttempt.attempt_number > (query.after_attempt_number or 0),
                    )
                    .order_by(QuestionAttempt.attempt_number)
                    .limit(query.limit + 1)
                ).mappings().all()
                included = rows[:query.limit]
                has_more = len(rows) > query.limit
                page = SelectedQuestion(
                    question_index=query.question_index,
                    attempts=[HistoryAttempt(
                        attempt_id=row["attempt_id"], attempt_number=row["attempt_number"],
                        answer_text=row["answer_text"], submitted_at=row["submitted_at"],
                        is_final=row["attempt_id"] == overview.final_attempt_id,
                        measurement=_measurement(row),
                    ) for row in included],
                    has_more=has_more,
                    next_after_attempt_number=included[-1]["attempt_number"] if has_more else None,
                )
            return HistoryDetail(summary=read.summary, questions=read.questions, selected_question=page)

    @staticmethod
    def _read_sessions(database: Session, identifiers: list[UUID]) -> dict[UUID, _SessionRead]:
        # Aggregate only named sessions. Neither retry rows nor answer text are
        # materialized for a summary; one latest row per practiced question joins.
        counts = (
            select(
                QuestionAttempt.session_id, QuestionAttempt.question_index,
                func.count(QuestionAttempt.id).label("attempt_count"),
                func.max(QuestionAttempt.attempt_number).label("latest_attempt_number"),
                func.max(QuestionAttempt.submitted_at).label("last_submitted_at"),
            )
            .where(QuestionAttempt.session_id.in_(identifiers))
            .group_by(QuestionAttempt.session_id, QuestionAttempt.question_index)
            .cte("history_question_counts")
        )
        rows = database.execute(
            select(
                StoredInterviewSession.id.label("session_id"), StoredInterviewSession.status,
                StoredInterviewSession.questions, StoredInterviewSession.current_question_index,
                StoredInterviewSession.created_at, StoredInterviewSession.completed_at,
                counts.c.question_index, counts.c.attempt_count,
                counts.c.latest_attempt_number, counts.c.last_submitted_at,
                QuestionAttempt.id.label("attempt_id"), QuestionAttempt.attempt_number,
                QuestionAttempt.submitted_at, QuestionAttempt.measurement_id,
                *_measurement_columns(),
            )
            .select_from(StoredInterviewSession)
            .outerjoin(counts, counts.c.session_id == StoredInterviewSession.id)
            .outerjoin(QuestionAttempt, and_(
                QuestionAttempt.session_id == counts.c.session_id,
                QuestionAttempt.question_index == counts.c.question_index,
                QuestionAttempt.attempt_number == counts.c.latest_attempt_number,
            ))
            .outerjoin(TranscriptionMeasurement, _measurement_join())
            .where(StoredInterviewSession.id.in_(identifiers))
            .order_by(StoredInterviewSession.id, counts.c.question_index)
        ).mappings().all()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["session_id"], []).append(row)
        return {identifier: HistoryReadService._project_session(items) for identifier, items in grouped.items()}

    @staticmethod
    def _project_session(rows) -> _SessionRead:
        first = rows[0]
        questions = first["questions"]
        current = first["current_question_index"]
        status = first["status"]
        completed = first["completed_at"]
        if (not isinstance(questions, (tuple, list)) or len(questions) != 5 or
                any(not isinstance(value, str) or not value.strip() for value in questions) or
                type(current) is not int or
                not ((status == "active" and 0 <= current < len(questions) and completed is None) or
                     (status == "completed" and current == len(questions) and completed is not None))):
            raise HistoryIntegrityError()
        per_question = {}
        for row in rows:
            index = row["question_index"]
            if index is None:
                continue
            if (type(index) is not int or not 0 <= index < len(questions) or
                    (status == "active" and index > current) or index in per_question or
                    row["attempt_count"] < 1 or row["attempt_id"] is None or
                    row["latest_attempt_number"] != row["attempt_number"]):
                raise HistoryIntegrityError()
            per_question[index] = row
        if any(index not in per_question for index in range(current)):
            raise HistoryIntegrityError()
        points = []
        overviews = []
        for index, question in enumerate(questions):
            row = per_question.get(index)
            finalized = index < current
            # Validate linked latest records even when their question is still open.
            measurement = _measurement(row) if row is not None else None
            if finalized:
                points.append(FinalizedPoint(
                    question_index=index, attempt_id=row["attempt_id"],
                    attempt_number=row["attempt_number"], submitted_at=row["submitted_at"],
                    measurement=measurement,
                ))
            overviews.append(QuestionOverview(
                question_index=index, question_text=question, finalized=finalized,
                attempt_count=row["attempt_count"] if row is not None else 0,
                latest_attempt_id=row["attempt_id"] if row is not None else None,
                latest_attempt_number=row["attempt_number"] if row is not None else None,
                final_attempt_id=row["attempt_id"] if finalized else None,
                final_attempt_number=row["attempt_number"] if finalized else None,
            ))
        last_submitted = max((row["last_submitted_at"] for row in per_question.values()), default=None)
        dates = [first["created_at"], *([last_submitted] if last_submitted is not None else []),
                 *([completed] if completed is not None else [])]
        if any(not isinstance(value, datetime) or value.tzinfo is None for value in dates):
            raise HistoryIntegrityError()
        if completed is not None and completed < first["created_at"]:
            raise HistoryIntegrityError()
        total_attempts = sum(row["attempt_count"] for row in per_question.values())
        return _SessionRead(
            summary=SessionSummary(
                session_id=first["session_id"], status=status, created_at=first["created_at"],
                completed_at=completed, current_question_number=current + 1 if status == "active" else None,
                total_questions=len(questions), finalized_question_count=current,
                questions_practiced_count=len(per_question), total_attempt_count=total_attempts,
                total_retry_count=total_attempts - len(per_question),
                measured_final_answer_count=sum(point.measurement is not None for point in points),
                last_submitted_at=last_submitted, last_saved_activity_at=max(dates), finalized_points=points,
            ),
            questions=overviews,
        )
