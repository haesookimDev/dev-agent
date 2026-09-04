"""Create the initial Kelpie control-plane schema.

Revision ID: 20260904_0001
Revises:
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPECTED_COLUMNS = {
    "agent_events": {
        "id",
        "work_item_id",
        "event_type",
        "source",
        "level",
        "message",
        "payload",
        "created_at",
    },
    "approvals": {"id", "work_item_id", "kind", "decision", "actor", "payload", "created_at"},
    "artifacts": {
        "id",
        "work_item_id",
        "kind",
        "name",
        "content_type",
        "object_key",
        "size_bytes",
        "created_at",
    },
    "console_leases": {
        "work_item_id",
        "holder_type",
        "holder",
        "version",
        "expires_at",
        "updated_at",
    },
    "delivery_bundles": {"work_item_id", "object_path", "sha256", "size_bytes", "created_at"},
    "delivery_jobs": {
        "work_item_id",
        "state",
        "attempts",
        "error",
        "created_at",
        "updated_at",
    },
    "feedback": {"id", "work_item_id", "actor", "channel", "message", "created_at"},
    "preview_endpoints": {
        "id",
        "work_item_id",
        "hostname",
        "target_url",
        "console_target_url",
        "expires_at",
        "created_at",
    },
    "resource_leases": {
        "id",
        "work_item_id",
        "worker_id",
        "token_hash",
        "state",
        "cpu",
        "memory_mb",
        "disk_gb",
        "expires_at",
        "created_at",
    },
    "webhook_deliveries": {"delivery_id", "event_name", "received_at"},
    "work_items": {
        "id",
        "source",
        "source_external_id",
        "title",
        "requirement",
        "repository",
        "status",
        "version",
        "requested_by",
        "assigned_worker_id",
        "budget_minutes",
        "budget_cost",
        "replan_limit",
        "approval_required",
        "github_installation_id",
        "github_issue_number",
        "pull_request_url",
        "created_at",
        "updated_at",
    },
    "worker_hosts": {
        "id",
        "name",
        "state",
        "cpu_total",
        "cpu_available",
        "memory_mb_total",
        "memory_mb_available",
        "disk_gb_available",
        "active_runs",
        "labels",
        "last_seen_at",
        "created_at",
    },
}


def existing_schema_is_legacy_baseline() -> bool:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    existing_kelpie_tables = existing_tables.intersection(EXPECTED_COLUMNS)
    if not existing_kelpie_tables:
        return False
    if existing_kelpie_tables != set(EXPECTED_COLUMNS):
        missing = sorted(set(EXPECTED_COLUMNS).difference(existing_kelpie_tables))
        raise RuntimeError(f"refusing to adopt a partial Kelpie schema; missing tables: {missing}")

    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if actual_columns != expected_columns:
            raise RuntimeError(
                f"refusing to adopt an incompatible Kelpie schema for {table_name}; "
                f"expected columns {sorted(expected_columns)}, got {sorted(actual_columns)}"
            )
    return True


def upgrade() -> None:
    if existing_schema_is_legacy_baseline():
        return

    work_source = sa.Enum("WEB", "GITHUB", "AUTONOMOUS", name="worksource")
    work_status = sa.Enum(
        "QUEUED",
        "PROVISIONING",
        "ANALYZING",
        "IMPLEMENTING",
        "VERIFYING",
        "AWAITING_FEEDBACK",
        "AWAITING_APPROVAL",
        "AWAITING_INPUT",
        "BUDGET_EXHAUSTED",
        "COMMITTING",
        "PR_CREATED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        name="workstatus",
    )
    worker_state = sa.Enum("ONLINE", "DRAINING", "OFFLINE", name="workerstate")

    op.create_table(
        "worker_hosts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("state", worker_state, nullable=False),
        sa.Column("cpu_total", sa.Integer(), nullable=False),
        sa.Column("cpu_available", sa.Integer(), nullable=False),
        sa.Column("memory_mb_total", sa.Integer(), nullable=False),
        sa.Column("memory_mb_available", sa.Integer(), nullable=False),
        sa.Column("disk_gb_available", sa.Integer(), nullable=False),
        sa.Column("active_runs", sa.Integer(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("event_name", sa.String(length=100), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", work_source, nullable=False),
        sa.Column("source_external_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("repository", sa.String(length=300), nullable=False),
        sa.Column("status", work_status, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("assigned_worker_id", sa.String(length=36), nullable=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=False),
        sa.Column("budget_cost", sa.String(length=32), nullable=True),
        sa.Column("replan_limit", sa.Integer(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("github_installation_id", sa.Integer(), nullable=True),
        sa.Column("github_issue_number", sa.Integer(), nullable=True),
        sa.Column("pull_request_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_worker_id"], ["worker_hosts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_external_id"),
    )
    op.create_index("ix_work_items_source", "work_items", ["source"])
    op.create_index("ix_work_items_status", "work_items", ["status"])
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_events_event_type", "agent_events", ["event_type"])
    op.create_index("ix_agent_events_work_id_id", "agent_events", ["work_item_id", "id"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_work_item_id", "approvals", ["work_item_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_work_item_id", "artifacts", ["work_item_id"])
    op.create_table(
        "console_leases",
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("holder_type", sa.String(length=16), nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("work_item_id"),
    )
    op.create_table(
        "delivery_bundles",
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("object_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("work_item_id"),
    )
    op.create_table(
        "delivery_jobs",
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("work_item_id"),
    )
    op.create_index("ix_delivery_jobs_state", "delivery_jobs", ["state"])
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_work_item_id", "feedback", ["work_item_id"])
    op.create_table(
        "preview_endpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("target_url", sa.String(length=1024), nullable=False),
        sa.Column("console_target_url", sa.String(length=1024), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_item_id"),
    )
    op.create_index("ix_preview_endpoints_hostname", "preview_endpoints", ["hostname"], unique=True)
    op.create_table(
        "resource_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("cpu", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_hosts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        sa.UniqueConstraint("work_item_id"),
    )
    op.create_index("ix_resource_leases_worker_id", "resource_leases", ["worker_id"])


def downgrade() -> None:
    raise RuntimeError(
        "the baseline migration cannot be downgraded because that would destroy all Kelpie data"
    )
