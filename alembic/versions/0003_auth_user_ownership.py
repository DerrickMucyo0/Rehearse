"""Authentication identities, digest-only sessions and nullable interview owners.

Revision ID: 0003_auth_user_ownership
Revises: 0002_pause_delivery_metrics

This historical DDL is self-contained, independent of evolving application models.
Existing interview sessions remain anonymous; no rows are backfilled or removed.
TODO(auth): This transitional schema is not deployable authentication. Identity
verification, session issuance and owner enforcement belong to later slices.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_auth_user_ownership"
down_revision = "0002_pause_delivery_metrics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("auth_provider", sa.Text(collation="C"), nullable=False),
        sa.Column("provider_subject", sa.Text(collation="C"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("auth_provider", "provider_subject", name="uq_users_auth_identity"),
        sa.CheckConstraint("auth_provider ~ '[^[:space:]]'", name="ck_users_auth_provider_nonblank"),
        sa.CheckConstraint("provider_subject ~ '[^[:space:]]'", name="ck_users_provider_subject_nonblank"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_context", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_sessions_user", ondelete="RESTRICT"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash_length"),
        sa.CheckConstraint(
            "request_context ~ '[^[:space:]]'", name="ck_auth_sessions_request_context_nonblank",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiration"),
    )
    # No default or backfill: historical anonymous interview sessions retain NULL.
    op.add_column("interview_sessions", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_interview_sessions_user", "interview_sessions", "users",
        ["user_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(
        "ix_interview_sessions_user_created_at", "interview_sessions",
        ["user_id", "created_at", "id"],
    )


def downgrade():
    # Remove only this extension; all pre-existing interview facts remain intact.
    op.drop_index("ix_interview_sessions_user_created_at", table_name="interview_sessions")
    op.drop_constraint("fk_interview_sessions_user", "interview_sessions", type_="foreignkey")
    op.drop_column("interview_sessions", "user_id")
    op.drop_table("auth_sessions")
    op.drop_table("users")
