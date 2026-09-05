"""Add organization membership and repository grants; isolate historical work."""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0004"
down_revision = "20260904_0003"
branch_labels = None
depends_on = None


def role_type():
    return sa.Enum("viewer", "operator", "approver", "administrator", native_enum=False,
                   name="iam_role")


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.String(1024), nullable=True),
        sa.Column("claim", sa.String(255), nullable=True),
        sa.UniqueConstraint("issuer", "claim", name="uq_organization_identity"),
    )
    op.execute(sa.text("INSERT INTO organizations (id) VALUES ('legacy')"))
    op.create_table(
        "principals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("issuer", sa.String(1024), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_principal_identity"),
    )
    op.create_table(
        "memberships",
        sa.Column("organization_id", sa.String(64), sa.ForeignKey("organizations.id"),
                  primary_key=True),
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), primary_key=True),
        sa.Column("role", role_type(), nullable=False),
        sa.CheckConstraint("role IN ('viewer', 'operator', 'approver', 'administrator')",
                           name="iam_role"),
    )
    op.create_table(
        "repositories",
        sa.Column("name", sa.String(300), primary_key=True),
        sa.Column("organization_id", sa.String(64), sa.ForeignKey("organizations.id"),
                  nullable=False),
        sa.Column("github_installation_id", sa.Integer(), nullable=True),
    )
    op.create_index("ix_repositories_organization_id", "repositories", ["organization_id"])
    op.create_table(
        "repository_grants",
        sa.Column("repository", sa.String(300), sa.ForeignKey("repositories.name"),
                  primary_key=True),
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), primary_key=True),
        sa.Column("role", role_type(), nullable=False),
        sa.CheckConstraint("role IN ('viewer', 'operator', 'approver', 'administrator')",
                           name="iam_role"),
    )
    op.create_table(
        "slack_identities",
        sa.Column("team_id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(255), primary_key=True),
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("organization_id", sa.String(64), sa.ForeignKey("organizations.id"),
                  nullable=False),
    )
    op.create_index("ix_slack_identities_organization_id", "slack_identities", ["organization_id"])
    with op.batch_alter_table("work_items") as batch:
        batch.add_column(sa.Column("organization_id", sa.String(64), nullable=False,
                                   server_default="legacy"))
        batch.create_foreign_key("fk_work_items_organization", "organizations",
                                 ["organization_id"], ["id"])
        batch.create_index("ix_work_items_organization_id", ["organization_id"])


def downgrade() -> None:
    with op.batch_alter_table("work_items") as batch:
        batch.drop_index("ix_work_items_organization_id")
        batch.drop_constraint("fk_work_items_organization", type_="foreignkey")
        batch.drop_column("organization_id")
    op.drop_table("slack_identities")
    op.drop_table("repository_grants")
    op.drop_table("repositories")
    op.drop_table("memberships")
    op.drop_table("principals")
    op.drop_table("organizations")
