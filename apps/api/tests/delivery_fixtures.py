"""Synthetic retained approval snapshots for isolated service/lock tests."""

import uuid

from app.models import AuditRecord, Role


async def seed_delivery_approval(session, work, digest, *, transport="web"):
    record = AuditRecord(
        organization_id=work.organization_id, work_item_id=work.id, repository=work.repository,
        action="approval.decided", target_id="1", actor_id=str(uuid.uuid4()),
        actor_subject="original-approver", identity_provider="https://identity.example",
        organization_role=Role.VIEWER, repository_role=Role.APPROVER,
        effective_role=Role.APPROVER, required_role=Role.APPROVER,
        request_id=str(uuid.uuid4()), correlation_id=work.correlation_id,
        source_ip="127.0.0.1", transport=transport, details={
            "kind": "pull_request", "decision": "approve", "delivery_queued": True,
            "delivery_bundle_sha256": digest, "work_status_after": "committing",
            "work_version_after": work.version,
        },
    )
    session.add(record)
    await session.flush()
    return record
