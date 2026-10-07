"""Persist deterministic practice scenarios on the session root.

Revision ID: 0005_session_scenarios
Revises: 0004_oidc_login_transactions

Existing sessions retain their question snapshots, owners and attempts. The
default classifies their original five-question flow as a job interview.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_session_scenarios"
down_revision = "0004_oidc_login_transactions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "interview_sessions",
        sa.Column(
            "scenario_type", sa.Text(collation="C"), nullable=False,
            server_default="job_interview",
        ),
    )
    op.create_check_constraint(
        "ck_sessions_scenario_type", "interview_sessions",
        "scenario_type IN ('job_interview', 'public_speaking', 'thesis_defense', 'salary_negotiation')",
    )


def downgrade():
    op.drop_constraint("ck_sessions_scenario_type", "interview_sessions", type_="check")
    op.drop_column("interview_sessions", "scenario_type")
