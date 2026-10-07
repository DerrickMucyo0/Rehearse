"""One-time OIDC redirect transactions with digest-only state.

Revision ID: 0004_oidc_login_transactions
Revises: 0003_auth_user_ownership

This historical DDL is self-contained, independent of evolving application models.
Existing identities, local auth sessions, owners and interview facts are unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_oidc_login_transactions"
down_revision = "0003_auth_user_ownership"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oidc_login_transactions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.Text(collation="C"), nullable=False),
        sa.Column("code_verifier", sa.Text(collation="C"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("state_hash", name="uq_oidc_login_transactions_state_hash"),
        sa.CheckConstraint(
            "octet_length(state_hash) = 32", name="ck_oidc_login_transactions_state_hash_length",
        ),
        sa.CheckConstraint("nonce ~ '[^[:space:]]'", name="ck_oidc_login_transactions_nonce_nonblank"),
        sa.CheckConstraint(
            "char_length(code_verifier) BETWEEN 43 AND 128 "
            "AND code_verifier !~ '[^A-Za-z0-9._~-]'",
            name="ck_oidc_login_transactions_code_verifier",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_oidc_login_transactions_expiration"),
    )
    op.create_index(
        "ix_oidc_login_transactions_expires_at", "oidc_login_transactions", ["expires_at"],
    )


def downgrade():
    op.drop_index("ix_oidc_login_transactions_expires_at", table_name="oidc_login_transactions")
    op.drop_table("oidc_login_transactions")
