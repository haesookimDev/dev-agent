"""Retain artifact expiration intent separately from completed file deletion."""

import sqlalchemy as sa
from alembic import context, op

from app.artifact_retention_schema import ARTIFACT_RETENTION_V1

revision = "20260906_0010"
down_revision = "20260906_0009"
branch_labels = None
depends_on = None


def lock_artifacts() -> None:
    if context.is_offline_mode():
        raise RuntimeError("artifact retention migration requires online state validation")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("LOCK TABLE artifacts IN ACCESS EXCLUSIVE MODE"))
    else:
        op.execute(sa.text("UPDATE artifacts SET size_bytes = size_bytes WHERE 0"))


def upgrade() -> None:
    lock_artifacts()
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("retention_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("retention_sha256", sa.String(64), nullable=True))
        batch.create_check_constraint("artifact_retention_state", ARTIFACT_RETENTION_V1)


def downgrade() -> None:
    lock_artifacts()
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM artifacts WHERE expired_at IS NOT NULL)"
    )):
        raise RuntimeError("downgrade would discard artifact expiration evidence")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_constraint("artifact_retention_state", type_="check")
        for name in ("retention_sha256", "retention_days", "purged_at", "expired_at"):
            batch.drop_column(name)
