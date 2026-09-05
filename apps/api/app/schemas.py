from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .models import Role, WorkerState, WorkSource, WorkStatus


class WorkItemCreate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    requirement: str = Field(min_length=3, max_length=100_000)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=300)
    budget_minutes: int = Field(default=240, ge=15, le=24 * 60)
    budget_cost: str | None = None

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value: str) -> str:
        return value.lower()


class WorkItemView(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    correlation_id: str
    source: WorkSource
    source_external_id: str | None
    title: str
    requirement: str
    repository: str
    status: WorkStatus
    version: int
    requested_by: str
    assigned_worker_id: str | None
    budget_minutes: int
    budget_cost: str | None
    replan_limit: int
    approval_required: bool
    github_installation_id: int | None
    github_issue_number: int | None
    pull_request_url: str | None
    created_at: datetime
    updated_at: datetime


class EventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=100)
    source: str = Field(default="control-plane", max_length=100)
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str = Field(default="", max_length=100_000)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventView(EventCreate):
    model_config = {"from_attributes": True}

    id: int
    work_item_id: str
    correlation_id: str
    created_at: datetime


class AuditRecordView(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    organization_id: str
    work_item_id: str
    repository: str
    action: str
    target_id: str
    actor_id: str | None
    actor_subject: str
    identity_provider: str
    organization_role: Role
    repository_role: Role | None
    effective_role: Role
    required_role: Role
    request_id: str
    correlation_id: str
    source_ip: str | None
    transport: Literal["web", "slack"]
    details: dict[str, Any]
    created_at: datetime


class TransitionRequest(BaseModel):
    status: WorkStatus
    expected_version: int
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkCancellationRequest(BaseModel):
    expected_version: int = Field(strict=True, ge=1)


class FeedbackCreate(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    channel: Literal["web", "slack"] = "web"


class ApprovalCreate(BaseModel):
    kind: Literal["pull_request", "budget", "console"]
    decision: Literal["approve", "reject"]
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkerRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    cpu_total: int = Field(ge=1)
    memory_mb_total: int = Field(ge=1024)
    disk_gb_available: int = Field(ge=1)
    labels: dict[str, str] = Field(default_factory=dict)


class WorkerHeartbeat(BaseModel):
    state: WorkerState = WorkerState.ONLINE
    cpu_available: int = Field(ge=0)
    memory_mb_available: int = Field(ge=0)
    disk_gb_available: int = Field(ge=0)
    active_runs: int = Field(ge=0)


class WorkerView(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    state: WorkerState
    cpu_total: int
    cpu_available: int
    memory_mb_total: int
    memory_mb_available: int
    disk_gb_available: int
    active_runs: int
    labels: dict[str, str]
    last_seen_at: datetime


class ClaimRequest(BaseModel):
    cpu: int = Field(default=2, ge=1, le=64)
    memory_mb: int = Field(default=4096, ge=1024)
    disk_gb: int = Field(default=30, ge=5)


class ClaimResponse(BaseModel):
    work_item: WorkItemView
    lease_token: str
    lease_expires_at: datetime


class ArtifactCreate(BaseModel):
    kind: str = Field(max_length=64)
    name: str = Field(max_length=255)
    content_type: str = Field(max_length=128)
    object_key: str = Field(max_length=1024)
    size_bytes: int = Field(default=0, ge=0)

    @field_validator("object_key")
    @classmethod
    def restrict_object_key(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("object_key must be a relative safe path")
        return value


class ArtifactView(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    work_item_id: str
    kind: str
    name: str
    content_type: str
    size_bytes: int
    created_at: datetime


class PreviewCreate(BaseModel):
    target_url: str = Field(pattern=r"^http://[A-Za-z0-9_.:-]+$")
    console_target_url: str | None = Field(default=None, pattern=r"^http://[A-Za-z0-9_.:-]+$")
    ttl_seconds: int = Field(default=86400, ge=300, le=172800)


class PreviewView(BaseModel):
    model_config = {"from_attributes": True}

    hostname: str
    target_url: str
    console_target_url: str | None
    expires_at: datetime


class ConsoleLeaseRequest(BaseModel):
    action: Literal["acquire", "release"]
    expected_version: int | None = None


class ConsoleLeaseView(BaseModel):
    model_config = {"from_attributes": True}

    work_item_id: str
    holder_type: str
    holder: str
    version: int
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
