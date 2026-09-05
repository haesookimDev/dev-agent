"""Persist session-bound, single-exchange preview grants.

Revision ID: 20260906_0008
Revises: 20260906_0007
"""

import sqlalchemy as sa
from alembic import op

revision = "20260906_0008"
down_revision = "20260906_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preview_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("work_item_id", sa.String(36), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("auth_session_hash", sa.String(64),
                  sa.ForeignKey("auth_sessions.token_hash", ondelete="CASCADE"), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("launch_hash", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("launch_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exchanged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("work_item_id", "auth_session_hash", "launch_hash", "token_hash", "expires_at"):
        op.create_index(f"ix_preview_grants_{column}", "preview_grants", [column],
                        unique=column in {"launch_hash", "token_hash"})


def downgrade() -> None:
    # Grants are disposable credentials, not audit records. Dropping them revokes access.
    op.drop_table("preview_grants")
