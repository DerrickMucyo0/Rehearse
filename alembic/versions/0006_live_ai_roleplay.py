"""Append-only adaptive questions with immutable persisted engine selection.

Revision ID: 0006_live_ai_roleplay
Revises: 0005_session_scenarios

Existing snapshots and all child/authentication facts remain unchanged. The
legacy default preserves deterministic dispatch for every pre-existing row.
This historical DDL is self-contained, independent of application models.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_live_ai_roleplay"
down_revision = "0005_session_scenarios"
branch_labels = None
depends_on = None

_LEGACY_SNAPSHOT = (
    "CASE WHEN jsonb_typeof(questions) = 'array' "
    "THEN jsonb_array_length(questions) = 5 ELSE false END"
)
_LEGACY_COMPLETION = (
    "CASE WHEN jsonb_typeof(questions) = 'array' THEN "
    "((status = 'active' AND current_question_index < jsonb_array_length(questions) "
    "AND completed_at IS NULL) OR "
    "(status = 'completed' AND current_question_index = jsonb_array_length(questions) "
    "AND completed_at IS NOT NULL)) ELSE false END"
)
_SNAPSHOT = (
    "CASE WHEN jsonb_typeof(questions) = 'array' "
    "THEN ((question_engine = 'deterministic-v1' AND jsonb_array_length(questions) = 5) OR "
    "(question_engine = 'live-ai-roleplay-v1' AND jsonb_array_length(questions) BETWEEN 1 AND 5)) "
    "ELSE false END"
)
_COMPLETION = (
    "CASE WHEN jsonb_typeof(questions) = 'array' THEN "
    "((status = 'active' AND current_question_index BETWEEN 0 AND 4 "
    "AND completed_at IS NULL AND "
    "((question_engine = 'deterministic-v1' AND current_question_index < jsonb_array_length(questions)) OR "
    "(question_engine = 'live-ai-roleplay-v1' AND jsonb_array_length(questions) = current_question_index + 1))) OR "
    "(status = 'completed' AND current_question_index = 5 AND jsonb_array_length(questions) = 5 "
    "AND completed_at IS NOT NULL)) ELSE false END"
)


def upgrade():
    op.add_column(
        "interview_sessions",
        sa.Column("question_engine", sa.Text(collation="C"), nullable=False, server_default="deterministic-v1"),
    )
    op.create_check_constraint(
        "ck_sessions_question_engine", "interview_sessions",
        "question_engine IN ('deterministic-v1', 'live-ai-roleplay-v1')",
    )
    for name in ("ck_sessions_question_snapshot", "ck_sessions_completion"):
        op.drop_constraint(name, "interview_sessions", type_="check")
    op.create_check_constraint("ck_sessions_question_snapshot", "interview_sessions", _SNAPSHOT)
    op.create_check_constraint("ck_sessions_completion", "interview_sessions", _COMPLETION)
    op.execute("""
        CREATE OR REPLACE FUNCTION rehearse_preserve_questions() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE old_length integer;
        BEGIN
            IF NEW.question_engine IS DISTINCT FROM OLD.question_engine THEN
                RAISE EXCEPTION 'Question engine is immutable.' USING ERRCODE = '23514';
            END IF;
            IF OLD.question_engine = 'deterministic-v1' THEN
                IF NEW.questions IS DISTINCT FROM OLD.questions THEN
                    RAISE EXCEPTION 'Question snapshots are immutable.' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            -- No adaptive row can reopen, skip, append separately from advancement,
            -- or complete before Q5. Unchanged state permits unrelated root updates.
            IF NEW.questions IS NOT DISTINCT FROM OLD.questions
               AND NEW.current_question_index IS NOT DISTINCT FROM OLD.current_question_index
               AND NEW.status IS NOT DISTINCT FROM OLD.status
               AND NEW.completed_at IS NOT DISTINCT FROM OLD.completed_at THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'active' AND OLD.current_question_index = 4
               AND NEW.status = 'completed' AND NEW.current_question_index = 5
               AND NEW.completed_at IS NOT NULL
               AND NEW.questions IS NOT DISTINCT FROM OLD.questions THEN
                RETURN NEW;
            END IF;
            IF jsonb_typeof(OLD.questions) = 'array' AND jsonb_typeof(NEW.questions) = 'array' THEN
                old_length := jsonb_array_length(OLD.questions);
                IF OLD.status = 'active' AND OLD.current_question_index BETWEEN 0 AND 3
                   AND old_length = OLD.current_question_index + 1
                   AND NEW.status = 'active' AND NEW.completed_at IS NULL
                   AND NEW.current_question_index = OLD.current_question_index + 1
                   AND jsonb_array_length(NEW.questions) = old_length + 1
                   AND (NEW.questions - old_length) = OLD.questions THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'Adaptive questions must append with one advancement.' USING ERRCODE = '23514';
        END;
        $$
    """)


def downgrade():
    # An adaptive prefix cannot become a truthful deterministic snapshot. Never
    # pad, delete, or silently reclassify live or completed adaptive sessions.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM interview_sessions WHERE question_engine = 'live-ai-roleplay-v1') THEN
                RAISE EXCEPTION 'Adaptive sessions prevent this downgrade.' USING ERRCODE = '23514';
            END IF;
        END $$
    """)
    for name in ("ck_sessions_completion", "ck_sessions_question_snapshot"):
        op.drop_constraint(name, "interview_sessions", type_="check")
    op.create_check_constraint("ck_sessions_question_snapshot", "interview_sessions", _LEGACY_SNAPSHOT)
    op.create_check_constraint("ck_sessions_completion", "interview_sessions", _LEGACY_COMPLETION)
    op.execute("""
        CREATE OR REPLACE FUNCTION rehearse_preserve_questions() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.questions IS DISTINCT FROM OLD.questions THEN
                RAISE EXCEPTION 'Question snapshots are immutable.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.drop_constraint("ck_sessions_question_engine", "interview_sessions", type_="check")
    op.drop_column("interview_sessions", "question_engine")
