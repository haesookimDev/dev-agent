"""Synthetic rows spanning every persisted contract; no real credentials or object contents."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from test_audit_storage import audit_values
from test_delivery_audit_schema import background_values

from app import models as m

ISSUER = "https://restore.example.invalid"
ORGANIZATION = "restore-one"
OTHER_ORGANIZATION = "restore-two"
REPOSITORY = "restore/service"


@dataclass(frozen=True)
class RestoreSeed:
    work_id: str
    other_id: str
    principal: str
    token: str = field(repr=False)


async def seed_database(database_url):
    engine = create_async_engine(database_url)
    identity, other, worker, principal = (str(uuid.uuid4()) for _ in range(4))
    expires = m.utcnow() + timedelta(hours=1)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        async with engine.begin() as connection:
            async def insert(model, **values):
                await connection.execute(sa.insert(model).values(**values))

            for organization in (ORGANIZATION, OTHER_ORGANIZATION):
                await insert(m.Organization, id=organization, issuer=ISSUER, claim=organization)
            await insert(m.Principal, id=principal, issuer=ISSUER, subject="restore-viewer")
            await insert(m.Membership, organization_id=ORGANIZATION, principal_id=principal,
                         role=m.Role.VIEWER)
            await insert(m.Repository, name=REPOSITORY, organization_id=ORGANIZATION,
                         github_installation_id=1)
            await insert(m.Repository, name="restore/private", organization_id=OTHER_ORGANIZATION)
            await insert(m.RepositoryGrant, repository=REPOSITORY, principal_id=principal,
                         role=m.Role.OPERATOR)
            await insert(m.SlackIdentity, team_id="restore-team", user_id="restore-user",
                         organization_id=ORGANIZATION, principal_id=principal)
            await insert(m.OIDCLoginAttempt, state_hash=token_hash, nonce=secrets.token_urlsafe(32),
                         code_verifier=secrets.token_urlsafe(32), expires_at=expires)
            await insert(m.AuthSession, token_hash=token_hash, subject="restore-viewer",
                         identity_provider=ISSUER, organization=ORGANIZATION, expires_at=expires)
            await insert(m.WorkerHost, id=worker, name="restore-worker", credential_required=True,
                         quarantined_at=m.utcnow(), state=m.WorkerState.OFFLINE,
                         cpu_total=4, cpu_available=2, memory_mb_total=8192,
                         memory_mb_available=4096, disk_gb_available=30, active_runs=1)
            credential = str(uuid.uuid4())
            await insert(m.WorkerCredential, id=credential, worker_id=worker,
                         token_hash=token_hash, expires_at=expires, revoked_at=m.utcnow())
            await insert(m.WorkerCredentialEvent, worker_id=worker, credential_id=credential,
                         action="revoked", actor="drill", reason="Synthetic recovery boundary")
            for key, organization, repository, status in (
                (identity, ORGANIZATION, REPOSITORY, m.WorkStatus.COMMITTING),
                (other, OTHER_ORGANIZATION, "restore/private", m.WorkStatus.QUEUED),
            ):
                await insert(m.WorkItem, id=key, organization_id=organization,
                             repository=repository,
                             source=m.WorkSource.WEB, title="복원 검증 / Restore drill",
                             requirement="Preserve ownership, approvals and audits",
                             status=status, assigned_worker_id=worker if key == identity else None,
                             github_installation_id=1, version=7, correlation_id=key)
            await insert(m.AgentEvent, work_item_id=identity, correlation_id=identity,
                         event_type="verification.completed", message="Synthetic evidence")
            await insert(m.ResourceLease, work_item_id=identity, worker_id=worker,
                         token_hash=token_hash, state="quarantined", expires_at=expires)
            await insert(m.Feedback, work_item_id=identity, actor="restore-viewer", message="검증")
            await insert(m.Approval, work_item_id=identity, kind="delivery", decision="approved",
                         actor="historical-approver", payload={"version": 7})
            await insert(m.Artifact, work_item_id=identity, kind="screenshot", name="screen.png",
                         content_type="image/png", object_key=f"{identity}/screen.png",
                         size_bytes=123)
            await insert(m.PreviewEndpoint, work_item_id=identity,
                         hostname="restore.example.invalid",
                         target_url="http://192.0.2.1:3000", expires_at=expires)
            await insert(m.ConsoleLease, work_item_id=identity, holder="restore-viewer",
                         holder_type="human", version=3, expires_at=expires)
            await insert(m.DeliveryBundle, work_item_id=identity,
                         object_path=f"/not-present/{identity}/patch.diff", sha256="a" * 64,
                         size_bytes=123)
            await insert(m.AuditRecord, **(audit_values() | {
                "id": 40, "organization_id": ORGANIZATION, "work_item_id": identity,
                "repository": REPOSITORY, "action": "approval.created",
                "actor_id": principal, "details": {"decision": "approved", "version": 7},
            }))
            await insert(m.AuditRecord, **(background_values() | {
                "id": 41, "organization_id": ORGANIZATION, "work_item_id": identity,
                "repository": REPOSITORY, "details": {"approval_audit_id": 40, "attempt": 1},
            }))
            await insert(m.DeliveryJob, work_item_id=identity, state="pending", attempts=1,
                         approval_audit_id=40)
            await insert(m.WebhookDelivery, delivery_id="restore-webhook", event_name="issues")
            await connection.exec_driver_sql(
                "SELECT setval(pg_get_serial_sequence('audit_records', 'id'), 41, true)",
            )
            # Non-default table ACL must survive. PUBLIC receives no data access.
            await connection.exec_driver_sql("REVOKE ALL ON audit_records FROM PUBLIC")
        return RestoreSeed(identity, other, principal, token)
    finally:
        await engine.dispose()
