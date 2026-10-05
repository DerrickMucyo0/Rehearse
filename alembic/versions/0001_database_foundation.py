"""Three-table PostgreSQL foundation; runtime session storage is unchanged.

Revision ID: 0001_database_foundation
Revises: None

This historical DDL is self-contained, independent of evolving application models.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_database_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("current_question_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(questions) = 'array' "
            "THEN jsonb_array_length(questions) = 5 ELSE false END",
            name="ck_sessions_question_snapshot",
        ),
        sa.CheckConstraint(
            "NOT jsonb_path_exists(questions, '$[*] ? (@.type() != \"string\" || @ == \"\")')",
            name="ck_sessions_question_text",
        ),
        sa.CheckConstraint("status IN ('active', 'completed')", name="ck_sessions_status"),
        sa.CheckConstraint("current_question_index >= 0", name="ck_sessions_nonnegative_index"),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(questions) = 'array' THEN "
            "((status = 'active' AND current_question_index < jsonb_array_length(questions) "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND current_question_index = jsonb_array_length(questions) "
            "AND completed_at IS NOT NULL)) ELSE false END",
            name="ck_sessions_completion",
        ),
        sa.CheckConstraint("completed_at IS NULL OR completed_at >= created_at", name="ck_sessions_dates"),
    )
    op.create_table(
        "transcription_measurements",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("measurement_version", sa.Text(), nullable=False),
        sa.Column("measurement_source", sa.Text(), nullable=False),
        sa.Column("recognized_word_count", sa.Integer(), nullable=False),
        sa.Column("um_count", sa.Integer(), nullable=True),
        sa.Column("uh_count", sa.Integer(), nullable=True),
        sa.Column("filler_unavailable_reason", sa.Text(), nullable=True),
        sa.Column("timed_utterance_span_seconds", sa.Double(), nullable=True),
        sa.Column("estimated_words_per_minute", sa.Double(), nullable=True),
        sa.Column("timing_unavailable_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["interview_sessions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "session_id", "question_index", name="uq_measurements_context"),
        sa.CheckConstraint("question_index >= 0", name="ck_measurements_question_index"),
        sa.CheckConstraint("length(btrim(measurement_version)) > 0", name="ck_measurements_version"),
        sa.CheckConstraint("measurement_source = 'original_transcription'", name="ck_measurements_source"),
        sa.CheckConstraint("recognized_word_count >= 0", name="ck_measurements_word_count"),
        sa.CheckConstraint("um_count IS NULL OR um_count >= 0", name="ck_measurements_um_count"),
        sa.CheckConstraint("uh_count IS NULL OR uh_count >= 0", name="ck_measurements_uh_count"),
        sa.CheckConstraint(
            "(filler_unavailable_reason IS NULL AND um_count IS NOT NULL AND uh_count IS NOT NULL) OR "
            "(filler_unavailable_reason IS NOT NULL AND filler_unavailable_reason = 'unsupported_language' "
            "AND um_count IS NULL AND uh_count IS NULL)", name="ck_measurements_filler_state",
        ),
        sa.CheckConstraint(
            "timed_utterance_span_seconds IS NULL OR (timed_utterance_span_seconds > 0 "
            "AND timed_utterance_span_seconds < 'Infinity'::double precision)",
            name="ck_measurements_finite_span",
        ),
        sa.CheckConstraint(
            "estimated_words_per_minute IS NULL OR (estimated_words_per_minute > 0 "
            "AND estimated_words_per_minute < 'Infinity'::double precision)",
            name="ck_measurements_finite_pace",
        ),
        sa.CheckConstraint(
            "timing_unavailable_reason IS NULL OR timing_unavailable_reason IN "
            "('missing_timings', 'timing_coverage_mismatch', 'invalid_timing', "
            "'invalid_timing_order', 'unusable_span')", name="ck_measurements_timing_reason",
        ),
        sa.CheckConstraint(
            "(timing_unavailable_reason IS NULL AND timed_utterance_span_seconds IS NOT NULL "
            "AND estimated_words_per_minute IS NOT NULL) OR "
            "(timing_unavailable_reason IS NOT NULL AND timed_utterance_span_seconds IS NULL "
            "AND estimated_words_per_minute IS NULL)", name="ck_measurements_timing_state",
        ),
    )
    op.create_index("ix_transcription_measurements_created_at", "transcription_measurements", ["created_at"])
    op.create_table(
        "question_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("measurement_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["interview_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["measurement_id", "session_id", "question_index"],
            ["transcription_measurements.id", "transcription_measurements.session_id",
             "transcription_measurements.question_index"],
            name="fk_attempts_measurement_context", ondelete="NO ACTION",
        ),
        sa.CheckConstraint("question_index >= 0", name="ck_attempts_question_index"),
        sa.CheckConstraint("attempt_number > 0", name="ck_attempts_number"),
        sa.CheckConstraint("char_length(answer_text) BETWEEN 1 AND 10000", name="ck_attempts_answer_length"),
        sa.UniqueConstraint("session_id", "question_index", "attempt_number", name="uq_attempts_number"),
        sa.UniqueConstraint("measurement_id", name="uq_attempts_measurement"),
    )
    # Narrow immutability rules. Progression and eligibility still belong to the
    # future transactional service; these triggers never inspect answer content.
    op.execute("""
        CREATE FUNCTION rehearse_preserve_questions() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.questions IS DISTINCT FROM OLD.questions THEN
                RAISE EXCEPTION 'Question snapshots are immutable.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_questions BEFORE UPDATE ON interview_sessions
        FOR EACH ROW EXECUTE FUNCTION rehearse_preserve_questions()
    """)
    op.execute("""
        CREATE FUNCTION rehearse_preserve_measurement() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Measurement snapshots are immutable.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_measurement BEFORE UPDATE ON transcription_measurements
        FOR EACH ROW EXECUTE FUNCTION rehearse_preserve_measurement()
    """)


def downgrade():
    # Child tables first. Dropping tables drops their triggers before functions.
    op.drop_table("question_attempts")
    op.drop_index("ix_transcription_measurements_created_at", table_name="transcription_measurements")
    op.drop_table("transcription_measurements")
    op.drop_table("interview_sessions")
    op.execute("DROP FUNCTION rehearse_preserve_measurement()")
    op.execute("DROP FUNCTION rehearse_preserve_questions()")
