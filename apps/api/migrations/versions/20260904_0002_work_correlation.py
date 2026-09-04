"""Persist work correlation identifiers.

Revision ID: 20260904_0002
Revises: 20260904_0001
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0002"
down_revision: str | None = "20260904_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("work_items", sa.Column("correlation_id", sa.String(length=36), nullable=True))
    op.execute(sa.text("UPDATE work_items SET correlation_id = id"))
    with op.batch_alter_table("work_items") as batch:
        batch.alter_column("correlation_id", existing_type=sa.String(length=36), nullable=False)
        batch.create_index("ix_work_items_correlation_id", ["correlation_id"], unique=False)

    op.add_column("agent_events", sa.Column("correlation_id", sa.String(length=36), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_events SET correlation_id = "
            "(SELECT work_items.correlation_id FROM work_items "
            "WHERE work_items.id = agent_events.work_item_id)"
        )
    )
    with op.batch_alter_table("agent_events") as batch:
        batch.alter_column("correlation_id", existing_type=sa.String(length=36), nullable=False)
        batch.create_index("ix_agent_events_correlation_id", ["correlation_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_events") as batch:
        batch.drop_index("ix_agent_events_correlation_id")
        batch.drop_column("correlation_id")
    with op.batch_alter_table("work_items") as batch:
        batch.drop_index("ix_work_items_correlation_id")
        batch.drop_column("correlation_id")
