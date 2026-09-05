"""Distinguish background audit actors and retain delivery approval provenance."""

import sqlalchemy as sa
from alembic import context, op

from app.audit_schema import AUDIT_ACTOR_ROLES_V2, AUDIT_GUARDS_V1

revision = "20260906_0009"
down_revision = "20260906_0007"
branch_labels = None
depends_on = None


def lock_tables() -> str:
    if context.is_offline_mode():
        raise RuntimeError("delivery audit migration requires an online emptiness check")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("LOCK TABLE delivery_jobs, audit_records IN ACCESS EXCLUSIVE MODE"))
    else:
        # Begin SQLite's write transaction before batch DDL; do not mutate retained rows.
        op.execute(sa.text("UPDATE delivery_jobs SET attempts = attempts WHERE 0"))
    return dialect


def restore_sqlite_guards(dialect: str) -> None:
    if dialect == "sqlite":
        for statement in AUDIT_GUARDS_V1["sqlite"]:
            op.execute(sa.text(statement))


def upgrade() -> None:
    dialect = lock_tables()
    with op.batch_alter_table("audit_records") as batch:
        batch.drop_constraint("audit_transport", type_="check")
        for name in ("organization_role", "effective_role", "required_role"):
            batch.alter_column(name, existing_type=sa.String(13), nullable=True)
        batch.create_check_constraint(
            "audit_transport", "transport IN ('web', 'slack', 'background')",
        )
        batch.create_check_constraint("audit_actor_roles", AUDIT_ACTOR_ROLES_V2)
    restore_sqlite_guards(dialect)
    op.add_column("delivery_jobs", sa.Column("approval_audit_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    dialect = lock_tables()
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM audit_records)")):
        raise RuntimeError("downgrade would destroy audit records; retain the schema and data")
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM delivery_jobs WHERE approval_audit_id IS NOT NULL)"
    )):
        raise RuntimeError("downgrade would destroy delivery approval provenance")
    with op.batch_alter_table("delivery_jobs") as batch:
        batch.drop_column("approval_audit_id")
    with op.batch_alter_table("audit_records") as batch:
        batch.drop_constraint("audit_actor_roles", type_="check")
        batch.drop_constraint("audit_transport", type_="check")
        for name in ("organization_role", "effective_role", "required_role"):
            batch.alter_column(name, existing_type=sa.String(13), nullable=False)
        batch.create_check_constraint("audit_transport", "transport IN ('web', 'slack')")
    restore_sqlite_guards(dialect)
