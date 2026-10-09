"""Persist the selected interviewer persona.

Revision ID: 0007_interviewer_personas
Revises: 0006_live_ai_roleplay
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_interviewer_personas"
down_revision = "0006_live_ai_roleplay"
branch_labels = None
depends_on = None


def upgrade():
    # Historical sessions stay NULL because no persona was selected for them.
    op.add_column(
        "interview_sessions",
        sa.Column("interviewer_persona_id", sa.Text(collation="C"), nullable=True),
    )
    op.create_check_constraint(
        "ck_sessions_interviewer_persona", "interview_sessions",
        "interviewer_persona_id IS NULL OR interviewer_persona_id IN ('recruiter', 'manager', 'hr')",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION rehearse_preserve_interviewer_persona() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.interviewer_persona_id IS DISTINCT FROM OLD.interviewer_persona_id THEN
                RAISE EXCEPTION 'Interviewer persona is immutable.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER preserve_interviewer_persona
        BEFORE UPDATE OF interviewer_persona_id ON interview_sessions
        FOR EACH ROW EXECUTE FUNCTION rehearse_preserve_interviewer_persona()
    """)


def downgrade():
    op.execute("DROP TRIGGER preserve_interviewer_persona ON interview_sessions")
    op.execute("DROP FUNCTION rehearse_preserve_interviewer_persona()")
    op.drop_constraint("ck_sessions_interviewer_persona", "interview_sessions", type_="check")
    op.drop_column("interview_sessions", "interviewer_persona_id")
