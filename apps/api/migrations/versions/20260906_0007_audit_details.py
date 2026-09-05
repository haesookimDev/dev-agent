"""Retain bounded, action-specific audit details without rewriting old snapshots."""

import sqlalchemy as sa
from alembic import context, op

from app.audit_schema import AUDIT_GUARDS_V1

revision = "20260906_0007"
down_revision = "20260906_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_records", sa.Column(
        "details", sa.JSON(), nullable=False, server_default="{}",
    ))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("audit downgrade requires an online emptiness check")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("LOCK TABLE audit_records IN ACCESS EXCLUSIVE MODE"))
    else:
        op.execute(sa.text("UPDATE audit_records SET id = id WHERE 0"))
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM audit_records)")):
        raise RuntimeError("downgrade would destroy audit records; retain the schema and data")
    with op.batch_alter_table("audit_records") as batch:
        batch.drop_column("details")
    if dialect == "sqlite":
        # Batch table recreation drops triggers; restore the previous revision's guards.
        for statement in AUDIT_GUARDS_V1["sqlite"]:
            op.execute(sa.text(statement))
