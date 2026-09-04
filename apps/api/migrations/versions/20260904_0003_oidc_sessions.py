"""Persist one-time OIDC login attempts and authenticated sessions.

Revision ID: 20260904_0003
Revises: 20260904_0002
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0003"
down_revision: str | None = "20260904_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oidc_login_attempts",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("return_to", sa.String(length=2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_index(
        "ix_oidc_login_attempts_expires_at",
        "oidc_login_attempts",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("identity_provider", sa.String(length=1024), nullable=False),
        sa.Column("organization", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_subject", "auth_sessions", ["subject"], unique=False)
    op.create_index(
        "ix_auth_sessions_organization", "auth_sessions", ["organization"], unique=False
    )
    op.create_index(
        "ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_organization", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_subject", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_oidc_login_attempts_expires_at", table_name="oidc_login_attempts")
    op.drop_table("oidc_login_attempts")
