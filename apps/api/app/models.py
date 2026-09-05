import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .audit_schema import register_audit_guards


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class WorkStatus(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    ANALYZING = "analyzing"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    AWAITING_FEEDBACK = "awaiting_feedback"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    BUDGET_EXHAUSTED = "budget_exhausted"
    COMMITTING = "committing"
    PR_CREATED = "pr_created"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkSource(StrEnum):
    WEB = "web"
    GITHUB = "github"
    AUTONOMOUS = "autonomous"


class WorkerState(StrEnum):
    ONLINE = "online"
    DRAINING = "draining"
    OFFLINE = "offline"


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    ADMINISTRATOR = "administrator"


def role_type() -> Enum:
    return Enum(Role, native_enum=False, name="iam_role",
                values_callable=lambda roles: [role.value for role in roles])


class AuditRecord(Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        Index("ix_audit_records_work_cursor", "organization_id", "work_item_id", "id"),
        CheckConstraint("transport IN ('web', 'slack')", name="audit_transport"),
    )

    # Identity and resource snapshots intentionally have no cascading foreign keys.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(64))
    work_item_id: Mapped[str] = mapped_column(String(36))
    repository: Mapped[str] = mapped_column(String(300))
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_subject: Mapped[str] = mapped_column(String(255))
    identity_provider: Mapped[str] = mapped_column(String(1024))
    organization_role: Mapped[Role] = mapped_column(role_type())
    repository_role: Mapped[Role | None] = mapped_column(role_type(), nullable=True)
    effective_role: Mapped[Role] = mapped_column(role_type())
    required_role: Mapped[Role] = mapped_column(role_type())
    request_id: Mapped[str] = mapped_column(String(36))
    correlation_id: Mapped[str] = mapped_column(String(36))
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    transport: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


register_audit_guards(AuditRecord.__table__)


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (UniqueConstraint("issuer", "claim", name="uq_organization_identity"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    claim: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_principal_identity"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    issuer: Mapped[str] = mapped_column(String(1024))
    subject: Mapped[str] = mapped_column(String(255))


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (CheckConstraint(
        "role IN ('viewer', 'operator', 'approver', 'administrator')", name="iam_role"
    ),)

    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    role: Mapped[Role] = mapped_column(role_type())


class Repository(Base):
    __tablename__ = "repositories"

    name: Mapped[str] = mapped_column(String(300), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    github_installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RepositoryGrant(Base):
    __tablename__ = "repository_grants"
    __table_args__ = (CheckConstraint(
        "role IN ('viewer', 'operator', 'approver', 'administrator')", name="iam_role"
    ),)

    repository: Mapped[str] = mapped_column(ForeignKey("repositories.name"), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    role: Mapped[Role] = mapped_column(role_type())


class SlackIdentity(Base):
    __tablename__ = "slack_identities"

    team_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)


class OIDCLoginAttempt(Base):
    __tablename__ = "oidc_login_attempts"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(255))
    code_verifier: Mapped[str] = mapped_column(String(128))
    return_to: Mapped[str] = mapped_column(String(2048), default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), index=True)
    identity_provider: Mapped[str] = mapped_column(String(1024))
    organization: Mapped[str] = mapped_column(String(255), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("organizations.id", name="fk_work_items_organization"),
        default="legacy", server_default="legacy", index=True,
    )
    correlation_id: Mapped[str] = mapped_column(
        String(36), index=True, default=lambda: str(uuid.uuid4())
    )
    source: Mapped[WorkSource] = mapped_column(Enum(WorkSource), index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    requirement: Mapped[str] = mapped_column(Text)
    repository: Mapped[str] = mapped_column(String(300))
    status: Mapped[WorkStatus] = mapped_column(
        Enum(WorkStatus), default=WorkStatus.QUEUED, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[str] = mapped_column(String(255), default="system")
    assigned_worker_id: Mapped[str | None] = mapped_column(
        ForeignKey("worker_hosts.id"), nullable=True
    )
    budget_minutes: Mapped[int] = mapped_column(Integer, default=240)
    budget_cost: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replan_limit: Mapped[int] = mapped_column(Integer, default=3)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    github_installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pull_request_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    events: Mapped[list["AgentEvent"]] = relationship(
        back_populates="work_item", cascade="all, delete-orphan"
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_work_id_id", "work_item_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id", ondelete="CASCADE"))
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(100), default="control-plane")
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    work_item: Mapped[WorkItem] = relationship(back_populates="events")


class WorkerHost(Base):
    __tablename__ = "worker_hosts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True)
    credential_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[WorkerState] = mapped_column(Enum(WorkerState), default=WorkerState.ONLINE)
    cpu_total: Mapped[int] = mapped_column(Integer)
    cpu_available: Mapped[int] = mapped_column(Integer)
    memory_mb_total: Mapped[int] = mapped_column(Integer)
    memory_mb_available: Mapped[int] = mapped_column(Integer)
    disk_gb_available: Mapped[int] = mapped_column(Integer)
    active_runs: Mapped[int] = mapped_column(Integer, default=0)
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerCredential(Base):
    __tablename__ = "worker_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_hosts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerCredentialEvent(Base):
    __tablename__ = "worker_credential_events"
    __table_args__ = (CheckConstraint(
        "action IN ('issued', 'rotated', 'revoked', 'quarantined')",
        name="worker_credential_action",
    ),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_hosts.id"), index=True)
    credential_id: Mapped[str | None] = mapped_column(
        ForeignKey("worker_credentials.id"), nullable=True,
    )
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResourceLease(Base):
    __tablename__ = "resource_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), unique=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_hosts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="active")
    cpu: Mapped[int] = mapped_column(Integer, default=2)
    memory_mb: Mapped[int] = mapped_column(Integer, default=4096)
    disk_gb: Mapped[int] = mapped_column(Integer, default=30)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(32), default="web")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreviewEndpoint(Base):
    __tablename__ = "preview_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), unique=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    target_url: Mapped[str] = mapped_column(String(1024))
    console_target_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsoleLease(Base):
    __tablename__ = "console_leases"

    work_item_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), primary_key=True)
    holder_type: Mapped[str] = mapped_column(String(16), default="agent")
    holder: Mapped[str] = mapped_column(String(255), default="agent")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DeliveryBundle(Base):
    __tablename__ = "delivery_bundles"

    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id"), primary_key=True
    )
    object_path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"

    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
