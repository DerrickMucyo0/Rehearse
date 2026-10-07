"""Owner-scoped, read-only projections of persisted practice facts.

These reads select exact attempt-linked measurements, never infer or recalculate
them. No answer text is loaded for summaries or question overviews.
"""
import base64
import binascii
import json
from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator,
)
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal
from app.comparisons import MeasurementSnapshot, delivery_snapshot
from app.database_models import QuestionAttempt, StoredInterviewSession, TranscriptionMeasurement
from app.roleplay import QuestionEngine
from app.scenarios import ScenarioType, questions_for_scenario
from app.sessions import SessionNotFound

Nonnegative = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, gt=0)]
HISTORY_DISCOVERY_DEFAULT_LIMIT = 10
HISTORY_DISCOVERY_MAX_LIMIT = 20


class HistoryIntegrityError(Exception):
    """Stored facts violate the supported lifecycle; never expose their values."""


class HistoryCursorError(ValueError):
    """Invalid continuation facts; never include supplied cursor content."""

    def __init__(self) -> None:
        super().__init__("Invalid history request.")


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


class HistoryDiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(strict=True, ge=1, le=HISTORY_DISCOVERY_MAX_LIMIT)] = HISTORY_DISCOVERY_DEFAULT_LIMIT
    cursor: Annotated[str, Field(strict=True, min_length=1, max_length=512)] | None = None


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
    scenario_type: ScenarioType = "job_interview"
    question_engine: QuestionEngine = "deterministic-v1"
    status: Literal["active", "completed"]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    current_question_number: Positive | None
    total_questions: Literal[5] = 5
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


class HistorySummaryPage(ReadModel):
    items: list[SessionSummary]
    next_cursor: str | None


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


def _encode_cursor(created_at: datetime, identifier: UUID) -> str:
    # Ordering facts only, never identity or authorization. Canonical encoding
    # makes malformed/noncanonical cursors fail without leaking their contents.
    payload = json.dumps(
        [created_at.astimezone(timezone.utc).isoformat(), str(identifier)],
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        facts = json.loads(raw)
        if (type(facts) is not list or len(facts) != 2 or
                any(type(value) is not str for value in facts)):
            raise ValueError()
        created_at, identifier = datetime.fromisoformat(facts[0]), UUID(facts[1])
        if created_at.tzinfo is None or _encode_cursor(created_at, identifier) != cursor:
            raise ValueError()
        return created_at, identifier
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError):
        raise HistoryCursorError() from None


class HistoryReadService:
    """Request-owned reads; authorize roots before querying their child facts."""

    def __init__(
        self, session_factory: Callable[[], Session], principal: AuthenticatedPrincipal,
    ) -> None:
        if type(principal) is not AuthenticatedPrincipal:
            raise TypeError("History reads require an authenticated principal.")
        self._session_factory = session_factory
        self._principal = principal

    @staticmethod
    def _read_only(database: Session) -> None:
        # Detail has two SELECTs. READ COMMITTED would permit different snapshots.
        # This applies only to this operation, never the pooled connection default.
        database.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

    def get_summaries(self, session_ids: list[UUID]) -> HistorySummaries:
        identifiers = HistoryBatchRequest(session_ids=session_ids).session_ids
        with self._session_factory() as database:
            self._read_only(database)
            owned = self._owned_identifiers(database, identifiers)
            reads = self._read_sessions(database, owned) if owned else {}
        summaries = sorted(reads.values(), key=lambda item: item.summary.session_id.int)
        summaries.sort(key=lambda item: item.summary.last_saved_activity_at, reverse=True)
        return HistorySummaries(
            summaries=[item.summary for item in summaries],
            missing_session_ids=[identifier for identifier in identifiers if identifier not in reads],
        )

    def get_discovery(self, limit: int = HISTORY_DISCOVERY_DEFAULT_LIMIT, cursor: str | None = None) -> HistorySummaryPage:
        query = HistoryDiscoveryQuery(limit=limit, cursor=cursor)
        continuation = _decode_cursor(query.cursor) if query.cursor is not None else None
        with self._session_factory() as database:
            self._read_only(database)
            statement = select(StoredInterviewSession.id, StoredInterviewSession.created_at).where(
                StoredInterviewSession.user_id == self._principal.user_id,
            )
            if continuation is not None:
                created_at, identifier = continuation
                statement = statement.where(or_(
                    StoredInterviewSession.created_at < created_at,
                    and_(StoredInterviewSession.created_at == created_at, StoredInterviewSession.id < identifier),
                ))
            # Stable keyset pagination, independent of mutable practice activity.
            roots = database.execute(statement.order_by(
                StoredInterviewSession.created_at.desc(), StoredInterviewSession.id.desc(),
            ).limit(query.limit + 1)).all()
            included = roots[:query.limit]
            reads = self._read_sessions(database, [row.id for row in included]) if included else {}
            return HistorySummaryPage(
                items=[reads[row.id].summary for row in included],
                next_cursor=_encode_cursor(included[-1].created_at, included[-1].id)
                if len(roots) > query.limit else None,
            )

    def _owned_identifiers(self, database: Session, identifiers: list[UUID]) -> list[UUID]:
        # Root-only authorization precedes any attempts or measurements query.
        return list(database.scalars(select(StoredInterviewSession.id).where(
            StoredInterviewSession.id.in_(identifiers),
            StoredInterviewSession.user_id == self._principal.user_id,
        )))

    def get_detail(
        self, session_id: UUID, question_index: int | None = None,
        after_attempt_number: int | None = None, limit: int = 10,
    ) -> HistoryDetail:
        query = HistoryDetailQuery(
            question_index=question_index, after_attempt_number=after_attempt_number, limit=limit,
        )
        with self._session_factory() as database:
            self._read_only(database)
            if not self._owned_identifiers(database, [session_id]):
                raise SessionNotFound()
            read = self._read_sessions(database, [session_id]).get(session_id)
            if read is None:
                raise SessionNotFound()
            page = None
            if query.question_index is not None:
                if query.question_index >= len(read.questions):
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

    def _read_sessions(self, database: Session, identifiers: list[UUID]) -> dict[UUID, _SessionRead]:
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
                StoredInterviewSession.scenario_type,
                StoredInterviewSession.question_engine,
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
            .where(
                StoredInterviewSession.id.in_(identifiers),
                StoredInterviewSession.user_id == self._principal.user_id,
            )
            .order_by(StoredInterviewSession.id, counts.c.question_index)
        ).mappings().all()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["session_id"], []).append(row)
        return {identifier: HistoryReadService._project_session(items) for identifier, items in grouped.items()}

    @staticmethod
    def _project_session(rows) -> _SessionRead:
        first = rows[0]
        scenario_type = first["scenario_type"]
        question_engine = first["question_engine"]
        try:
            questions_for_scenario(scenario_type)
        except ValueError:
            raise HistoryIntegrityError() from None
        questions = first["questions"]
        current = first["current_question_index"]
        status = first["status"]
        completed = first["completed_at"]
        if (question_engine not in ("deterministic-v1", "live-ai-roleplay-v1") or
                not isinstance(questions, (tuple, list)) or not 1 <= len(questions) <= 5 or
                (question_engine == "deterministic-v1" and len(questions) != 5) or
                any(not isinstance(value, str) or not value.strip() for value in questions) or
                type(current) is not int or
                not ((status == "active" and 0 <= current < len(questions) and completed is None and
                      (question_engine == "deterministic-v1" or len(questions) == current + 1)) or
                     (status == "completed" and current == 5 and len(questions) == 5 and completed is not None))):
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
                session_id=first["session_id"], scenario_type=scenario_type, question_engine=question_engine,
                status=status, created_at=first["created_at"],
                completed_at=completed, current_question_number=current + 1 if status == "active" else None,
                total_questions=5, finalized_question_count=current,
                questions_practiced_count=len(per_question), total_attempt_count=total_attempts,
                total_retry_count=total_attempts - len(per_question),
                measured_final_answer_count=sum(point.measurement is not None for point in points),
                last_submitted_at=last_submitted, last_saved_activity_at=max(dates), finalized_points=points,
            ),
            questions=overviews,
        )
