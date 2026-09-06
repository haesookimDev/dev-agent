"""Append-only service execution records linked to immutable human approval evidence."""

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditRecord, DeliveryBundle, DeliveryJob, Role, WorkItem


class DeliveryAuthorityError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeliveryAuthority:
    audit_id: int
    bundle_sha256: str
    work_version: int


async def delivery_authority(
    session: AsyncSession, item: WorkItem, job: DeliveryJob, bundle: DeliveryBundle | None,
) -> DeliveryAuthority:
    if job.approval_audit_id is None:
        raise DeliveryAuthorityError("approval_unavailable")
    source = await session.get(AuditRecord, job.approval_audit_id)
    if source is None:
        raise DeliveryAuthorityError("approval_unavailable")
    if bundle is None:
        raise DeliveryAuthorityError("bundle_unavailable")
    details = source.details
    if not isinstance(details, dict):
        raise DeliveryAuthorityError("approval_mismatch")
    digest = details.get("delivery_bundle_sha256")
    version = details.get("work_version_after")
    if (
        source.action != "approval.decided" or source.transport not in {"web", "slack"}
        or source.organization_id != item.organization_id or source.repository != item.repository
        or source.work_item_id != item.id or source.correlation_id != item.correlation_id
        or source.required_role != Role.APPROVER
        or source.effective_role not in {Role.APPROVER, Role.ADMINISTRATOR}
        or details.get("kind") != "pull_request" or details.get("decision") != "approve"
        or details.get("delivery_queued") is not True
        or details.get("work_status_after") != "committing"
        or type(version) is not int or version != item.version
        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or digest != bundle.sha256
    ):
        raise DeliveryAuthorityError("approval_mismatch")
    return DeliveryAuthority(source.id, digest, version)


def pull_request_number(url: str | None, repository: str) -> int | None:
    # Never copy arbitrary upstream URLs, query strings, tokens or error text into the audit.
    match = re.fullmatch(rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]{{0,9}})",
                         url or "")
    return int(match[1]) if match else None


def record_delivery_audit(
    session: AsyncSession, item: WorkItem, job: DeliveryJob, *, action: str,
    request_id: str, attempt: int, authority: DeliveryAuthority | None, stage: str,
    publication: str = "not_started", error_code: str | None = None,
    pull_request_url: str | None = None,
) -> None:
    session.add(AuditRecord(
        organization_id=item.organization_id, work_item_id=item.id, repository=item.repository,
        action=action, target_id=item.id, actor_id=None, actor_subject="delivery:github",
        identity_provider="urn:kelpie:service", organization_role=None, repository_role=None,
        effective_role=None, required_role=None, source_ip=None, transport="background",
        request_id=request_id, correlation_id=item.correlation_id, details={
            "approval_audit_id": authority.audit_id if authority else None,
            "authorization": ("denied" if error_code in {
                "approval_unavailable", "approval_mismatch", "bundle_unavailable",
                "bundle_integrity_failed",
            } else "verified" if authority else "unavailable"),
            "approved_bundle_sha256": authority.bundle_sha256 if authority else None,
            "approved_work_version": authority.work_version if authority else None,
            "attempt": attempt, "worker_id": item.assigned_worker_id,
            "work_status": item.status.value, "work_version": item.version,
            "job_state": job.state, "stage": stage, "error_code": error_code,
            "publication": publication,
            "pull_request_number": pull_request_number(pull_request_url, item.repository),
        },
    ))
