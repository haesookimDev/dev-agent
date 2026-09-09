"""Persist scoped progress for scheduled ordinary-artifact retention."""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0011"
down_revision = "20260906_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_retention_jobs",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("cursor", sa.String(36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sweep_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(scope_key) = 64", name="retention_job_scope"),
        sa.CheckConstraint("version >= 1", name="retention_job_version"),
        sa.CheckConstraint("cursor IS NULL OR length(cursor) = 36", name="retention_job_cursor"),
    )


def downgrade() -> None:
    # Stop the scheduler first. Only resumable scan progress is discarded; the
    # artifact expiration journal and append-only audits remain in revision 0010.
    op.drop_table("artifact_retention_jobs")
