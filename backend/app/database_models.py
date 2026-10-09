"""PostgreSQL persistence mappings, separate from public API response models.

    Published questions and measurements are immutable in PostgreSQL (migration
    triggers). Adaptive questions append only alongside progression. Checks belong to the
    transactional session service, as does explicit measurement attachment.
    Background cleanup remains a future service responsibility.
"""
from datetime import datetime, timedelta
from re import fullmatch
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, DateTime, Double, ForeignKey, ForeignKeyConstraint, Index,
    Integer, LargeBinary, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates
from sqlalchemy.types import TypeDecorator

from app.scenarios import ScenarioType, questions_for_scenario
from app.roleplay import QuestionEngine
from app.interviewer_personas import InterviewerPersonaId, persona_for

MEASUREMENT_VERSION = "speaking-metrics-v1"
UNLINKED_MEASUREMENT_RETENTION = timedelta(hours=24)


def validate_submitted_answer_text(value: str) -> str:
    """Shared submission policy, enforced before PostgreSQL insertion."""
    if not isinstance(value, str):
        raise ValueError("Submitted answer must be text.")
    if "\x00" in value:
        raise ValueError("Submitted answer must not contain U+0000.")
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= 10000:
        raise ValueError("Submitted answer must contain 1–10000 trimmed characters.")
    return trimmed


class QuestionSnapshot(TypeDecorator):
    """An immutable Python tuple backed by PostgreSQL JSONB, without aliasing."""
    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return list(value) if isinstance(value, tuple) else value

    def process_result_value(self, value, dialect):
        return tuple(value) if value is not None else None


class Base(DeclarativeBase):
    pass


def _validate_auth_text(value: str) -> str:
    """Validate opaque authentication text without normalizing or exposing it."""
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError("Authentication text must be nonblank text without U+0000.")
    return value


# TODO(auth): This transitional schema is not deployable authentication. Trusted
# identity verification, session issuance and owner enforcement belong to later slices.
class User(Base):
    """Identity keyed by the exact trusted issuer and opaque provider subject."""
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("auth_provider", "provider_subject", name="uq_users_auth_identity"),
        CheckConstraint("auth_provider ~ '[^[:space:]]'", name="ck_users_auth_provider_nonblank"),
        CheckConstraint("provider_subject ~ '[^[:space:]]'", name="ck_users_provider_subject_nonblank"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    auth_provider: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    provider_subject: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    @validates("auth_provider", "provider_subject")
    def validate_identity(self, key, value):
        return _validate_auth_text(value)


class AuthSession(Base):
    """A digest-only credential record with a login-bound request context."""
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash_length"),
        CheckConstraint(
            "request_context ~ '[^[:space:]]'", name="ck_auth_sessions_request_context_nonblank",
        ),
        CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiration"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # The future credential store hashes the raw credential to a SHA-256 digest.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", name="fk_auth_sessions_user", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # A future login flow must issue an unpredictable context for each login.
    request_context: Mapped[str] = mapped_column(Text, nullable=False)

    @validates("token_hash")
    def validate_token_hash(self, key, value):
        if type(value) is not bytes or len(value) != 32:
            raise ValueError("Authentication token hash must be exactly 32 bytes.")
        return value

    @validates("request_context")
    def validate_request_context(self, key, value):
        return _validate_auth_text(value)


class OIDCLoginTransaction(Base):
    """One-time redirect state with server-side nonce and PKCE verifier.

    State is persisted only as its SHA-256 digest; no identity is known yet.
    Expiry and atomic consumption belong to the dedicated login store.
    """
    __tablename__ = "oidc_login_transactions"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_oidc_login_transactions_state_hash"),
        CheckConstraint(
            "octet_length(state_hash) = 32", name="ck_oidc_login_transactions_state_hash_length",
        ),
        CheckConstraint("nonce ~ '[^[:space:]]'", name="ck_oidc_login_transactions_nonce_nonblank"),
        CheckConstraint(
            "char_length(code_verifier) BETWEEN 43 AND 128 "
            "AND code_verifier !~ '[^A-Za-z0-9._~-]'",
            name="ck_oidc_login_transactions_code_verifier",
        ),
        CheckConstraint("expires_at > created_at", name="ck_oidc_login_transactions_expiration"),
        Index("ix_oidc_login_transactions_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text(collation="C"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @validates("state_hash")
    def validate_state_hash(self, key, value):
        if type(value) is not bytes or len(value) != 32:
            raise ValueError("OIDC login state hash must be exactly 32 bytes.")
        return value

    @validates("nonce")
    def validate_nonce(self, key, value):
        return _validate_auth_text(value)

    @validates("code_verifier")
    def validate_code_verifier(self, key, value):
        if type(value) is not str or fullmatch(r"[A-Za-z0-9._~-]{43,128}", value) is None:
            raise ValueError("OIDC PKCE verifier must contain 43–128 ASCII unreserved characters.")
        return value


class StoredInterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        CheckConstraint(
            "scenario_type IN ('job_interview', 'public_speaking', 'thesis_defense', 'salary_negotiation')",
            name="ck_sessions_scenario_type",
        ),
        CheckConstraint(
            "question_engine IN ('deterministic-v1', 'live-ai-roleplay-v1')",
            name="ck_sessions_question_engine",
        ),
        CheckConstraint(
            "interviewer_persona_id IS NULL OR interviewer_persona_id IN ('recruiter', 'manager', 'hr')",
            name="ck_sessions_interviewer_persona",
        ),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(questions) = 'array' "
            "THEN ((question_engine = 'deterministic-v1' AND jsonb_array_length(questions) = 5) OR "
            "(question_engine = 'live-ai-roleplay-v1' AND jsonb_array_length(questions) BETWEEN 1 AND 5)) "
            "ELSE false END",
            name="ck_sessions_question_snapshot",
        ),
        CheckConstraint(
            "NOT jsonb_path_exists(questions, '$[*] ? (@.type() != \"string\" || @ == \"\")')",
            name="ck_sessions_question_text",
        ),
        CheckConstraint("status IN ('active', 'completed')", name="ck_sessions_status"),
        CheckConstraint("current_question_index >= 0", name="ck_sessions_nonnegative_index"),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(questions) = 'array' THEN "
            "((status = 'active' AND current_question_index BETWEEN 0 AND 4 "
            "AND completed_at IS NULL AND "
            "((question_engine = 'deterministic-v1' AND current_question_index < jsonb_array_length(questions)) OR "
            "(question_engine = 'live-ai-roleplay-v1' AND jsonb_array_length(questions) = current_question_index + 1))) OR "
            "(status = 'completed' AND current_question_index = 5 AND jsonb_array_length(questions) = 5 "
            "AND completed_at IS NOT NULL)) ELSE false END",
            name="ck_sessions_completion",
        ),
        CheckConstraint("completed_at IS NULL OR completed_at >= created_at", name="ck_sessions_dates"),
        Index("ix_interview_sessions_user_created_at", "user_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # TODO(auth): Keep anonymous sessions valid until a later slice enforces ownership.
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", name="fk_interview_sessions_user", ondelete="RESTRICT"),
        nullable=True,
    )
    questions: Mapped[tuple[str, ...]] = mapped_column(QuestionSnapshot(), nullable=False)
    question_engine: Mapped[QuestionEngine] = mapped_column(
        Text(collation="C"), nullable=False, default="deterministic-v1", server_default="deterministic-v1",
    )
    scenario_type: Mapped[ScenarioType] = mapped_column(
        Text(collation="C"), nullable=False, default="job_interview", server_default="job_interview",
    )
    interviewer_persona_id: Mapped[InterviewerPersonaId | None] = mapped_column(
        Text(collation="C"), nullable=True,
    )
    current_question_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("questions")
    def validate_questions(self, key, value):
        if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 5 or
                any(not isinstance(item, str) or not item.strip() or "\x00" in item for item in value)):
            raise ValueError("Question snapshot must contain one to five nonempty text questions.")
        return tuple(value)

    @validates("question_engine")
    def validate_question_engine(self, key, value):
        if value not in ("deterministic-v1", "live-ai-roleplay-v1"):
            raise ValueError("Unsupported question engine.")
        return value

    @validates("scenario_type")
    def validate_scenario_type(self, key, value):
        questions_for_scenario(value)
        return value

    @validates("interviewer_persona_id")
    def validate_interviewer_persona_id(self, key, value):
        if value is not None:
            persona_for(value)
        return value


class TranscriptionMeasurement(Base):
    __tablename__ = "transcription_measurements"
    __table_args__ = (
        UniqueConstraint("id", "session_id", "question_index", name="uq_measurements_context"),
        CheckConstraint("question_index >= 0", name="ck_measurements_question_index"),
        CheckConstraint("length(btrim(measurement_version)) > 0", name="ck_measurements_version"),
        CheckConstraint("measurement_source = 'original_transcription'", name="ck_measurements_source"),
        CheckConstraint("recognized_word_count >= 0", name="ck_measurements_word_count"),
        CheckConstraint("um_count IS NULL OR um_count >= 0", name="ck_measurements_um_count"),
        CheckConstraint("uh_count IS NULL OR uh_count >= 0", name="ck_measurements_uh_count"),
        CheckConstraint(
            "(filler_unavailable_reason IS NULL AND um_count IS NOT NULL AND uh_count IS NOT NULL) OR "
            "(filler_unavailable_reason IS NOT NULL AND filler_unavailable_reason = 'unsupported_language' "
            "AND um_count IS NULL AND uh_count IS NULL)",
            name="ck_measurements_filler_state",
        ),
        CheckConstraint(
            "timed_utterance_span_seconds IS NULL OR (timed_utterance_span_seconds > 0 "
            "AND timed_utterance_span_seconds < 'Infinity'::double precision)",
            name="ck_measurements_finite_span",
        ),
        CheckConstraint(
            "estimated_words_per_minute IS NULL OR (estimated_words_per_minute > 0 "
            "AND estimated_words_per_minute < 'Infinity'::double precision)",
            name="ck_measurements_finite_pace",
        ),
        CheckConstraint(
            "timing_unavailable_reason IS NULL OR timing_unavailable_reason IN "
            "('missing_timings', 'timing_coverage_mismatch', 'invalid_timing', "
            "'invalid_timing_order', 'unusable_span')",
            name="ck_measurements_timing_reason",
        ),
        CheckConstraint(
            "(timing_unavailable_reason IS NULL AND timed_utterance_span_seconds IS NOT NULL "
            "AND estimated_words_per_minute IS NOT NULL) OR "
            "(timing_unavailable_reason IS NOT NULL AND timed_utterance_span_seconds IS NULL "
            "AND estimated_words_per_minute IS NULL)",
            name="ck_measurements_timing_state",
        ),
        CheckConstraint(
            "CASE WHEN delivery_measurement_version IS NULL THEN "
            "(pause_count IS NULL AND total_pause_duration_seconds IS NULL "
            "AND longest_pause_seconds IS NULL AND pause_unavailable_reason IS NULL) "
            "WHEN pause_unavailable_reason IS NULL THEN "
            "(pause_count IS NOT NULL AND total_pause_duration_seconds IS NOT NULL "
            "AND longest_pause_seconds IS NOT NULL) ELSE "
            "(pause_count IS NULL AND total_pause_duration_seconds IS NULL "
            "AND longest_pause_seconds IS NULL) END",
            name="ck_measurements_delivery_state",
        ),
        CheckConstraint(
            "delivery_measurement_version IS NULL OR "
            "delivery_measurement_version ~ '[^[:space:]]'",
            name="ck_measurements_delivery_version",
        ),
        CheckConstraint(
            "pause_unavailable_reason IS NULL OR pause_unavailable_reason IN "
            "('missing_timings', 'timing_coverage_mismatch', 'invalid_timing', "
            "'invalid_timing_order', 'unusable_span')",
            name="ck_measurements_pause_reason",
        ),
        CheckConstraint("pause_count IS NULL OR pause_count >= 0", name="ck_measurements_pause_count"),
        CheckConstraint(
            "total_pause_duration_seconds IS NULL OR (total_pause_duration_seconds >= 0 "
            "AND total_pause_duration_seconds < 'Infinity'::double precision)",
            name="ck_measurements_finite_pause_total",
        ),
        CheckConstraint(
            "longest_pause_seconds IS NULL OR (longest_pause_seconds >= 0 "
            "AND longest_pause_seconds < 'Infinity'::double precision)",
            name="ck_measurements_finite_pause_longest",
        ),
        CheckConstraint(
            "CASE WHEN pause_count IS NULL THEN true ELSE "
            "(total_pause_duration_seconds IS NOT NULL AND longest_pause_seconds IS NOT NULL AND "
            "((pause_count = 0 AND total_pause_duration_seconds = 0 AND longest_pause_seconds = 0) OR "
            "(pause_count > 0 AND total_pause_duration_seconds > 0 AND longest_pause_seconds > 0 "
            "AND longest_pause_seconds <= total_pause_duration_seconds))) END",
            name="ck_measurements_pause_durations",
        ),
        CheckConstraint(
            "CASE WHEN pause_count IS NULL THEN true "
            "WHEN recognized_word_count >= 1 THEN pause_count <= recognized_word_count - 1 "
            "ELSE false END",
            name="ck_measurements_pause_word_bound",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    question_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    measurement_version: Mapped[str] = mapped_column(Text, default=MEASUREMENT_VERSION)
    measurement_source: Mapped[str] = mapped_column(Text, default="original_transcription")
    recognized_word_count: Mapped[int] = mapped_column(Integer)
    um_count: Mapped[int | None] = mapped_column(Integer)
    uh_count: Mapped[int | None] = mapped_column(Integer)
    filler_unavailable_reason: Mapped[str | None] = mapped_column(Text)
    timed_utterance_span_seconds: Mapped[float | None] = mapped_column(Double)
    estimated_words_per_minute: Mapped[float | None] = mapped_column(Double)
    timing_unavailable_reason: Mapped[str | None] = mapped_column(Text)
    # No defaults: older snapshots truthfully retain an unrecorded delivery extension.
    delivery_measurement_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_pause_duration_seconds: Mapped[float | None] = mapped_column(Double, nullable=True)
    longest_pause_seconds: Mapped[float | None] = mapped_column(Double, nullable=True)
    pause_unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def unlinked_deletion_eligible(self, *, linked: bool, now: datetime) -> bool:
        """Pure policy helper; a future cleanup transaction must recheck linkage."""
        if self.created_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("Measurement retention requires timezone-aware timestamps.")
        return not linked and now >= self.created_at + UNLINKED_MEASUREMENT_RETENTION


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    __table_args__ = (
        CheckConstraint("question_index >= 0", name="ck_attempts_question_index"),
        CheckConstraint("attempt_number > 0", name="ck_attempts_number"),
        CheckConstraint("char_length(answer_text) BETWEEN 1 AND 10000", name="ck_attempts_answer_length"),
        UniqueConstraint("session_id", "question_index", "attempt_number", name="uq_attempts_number"),
        UniqueConstraint("measurement_id", name="uq_attempts_measurement"),
        ForeignKeyConstraint(
            ["measurement_id", "session_id", "question_index"],
            ["transcription_measurements.id", "transcription_measurements.session_id",
             "transcription_measurements.question_index"],
            name="fk_attempts_measurement_context", ondelete="NO ACTION",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("interview_sessions.id", ondelete="CASCADE"))
    question_index: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    answer_text: Mapped[str] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    measurement_id: Mapped[UUID | None] = mapped_column()

    @validates("answer_text")
    def validate_answer(self, key, value):
        return validate_submitted_answer_text(value)
