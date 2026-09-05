"""Add append-only identity and role-decision audit snapshots."""

import sqlalchemy as sa
from alembic import context, op

from app.audit_schema import AUDIT_GUARDS_V1

revision = "20260906_0006"
down_revision = "20260905_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("work_item_id", sa.String(36), nullable=False),
        sa.Column("repository", sa.String(300), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=True),
        sa.Column("actor_subject", sa.String(255), nullable=False),
        sa.Column("identity_provider", sa.String(1024), nullable=False),
        sa.Column("organization_role", sa.String(13), nullable=False),
        sa.Column("repository_role", sa.String(13), nullable=True),
        sa.Column("effective_role", sa.String(13), nullable=False),
        sa.Column("required_role", sa.String(13), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("source_ip", sa.String(45), nullable=True),
        sa.Column("transport", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("transport IN ('web', 'slack')", name="audit_transport"),
    )
    op.create_index("ix_audit_records_work_cursor", "audit_records",
                    ["organization_id", "work_item_id", "id"])
    for statement in AUDIT_GUARDS_V1[op.get_bind().dialect.name]:
        op.execute(sa.text(statement))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("audit downgrade requires an online emptiness check")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("LOCK TABLE audit_records IN ACCESS EXCLUSIVE MODE"))
    else:
        # Acquire SQLite's writer lock without modifying rows or firing row triggers.
        op.execute(sa.text("UPDATE audit_records SET id = id WHERE 0"))
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM audit_records)")):
        raise RuntimeError("downgrade would destroy audit records; retain the schema and data")
    op.drop_table("audit_records")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION kelpie_audit_immutable_v1()"))
