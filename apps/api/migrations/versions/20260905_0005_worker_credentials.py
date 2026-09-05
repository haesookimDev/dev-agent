"""Add independently revocable worker credentials and quarantine metadata."""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0005"
down_revision = "20260905_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_hosts", sa.Column(
        "credential_required", sa.Boolean(), nullable=False, server_default="0",
    ))
    op.add_column("worker_hosts", sa.Column(
        "quarantined_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.create_table(
        "worker_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("worker_id", sa.String(36), sa.ForeignKey("worker_hosts.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_worker_credentials_worker_id", "worker_credentials", ["worker_id"])
    op.create_table(
        "worker_credential_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("worker_id", sa.String(36), sa.ForeignKey("worker_hosts.id"), nullable=False),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("worker_credentials.id"),
                  nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('issued', 'rotated', 'revoked', 'quarantined')",
                           name="worker_credential_action"),
    )
    op.create_index(
        "ix_worker_credential_events_worker_id", "worker_credential_events", ["worker_id"],
    )


def downgrade() -> None:
    op.drop_table("worker_credential_events")
    op.drop_table("worker_credentials")
    with op.batch_alter_table("worker_hosts") as batch:
        batch.drop_column("quarantined_at")
        batch.drop_column("credential_required")
