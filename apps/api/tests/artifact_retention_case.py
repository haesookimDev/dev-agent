"""Owned synthetic retention case shared by SQLite and real PostgreSQL tests."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import artifact_retention as retention
from app import models as m
from app.artifact_storage import write_artifact_content


@dataclass
class RetentionCase:
    sessions: async_sessionmaker
    root: Path
    work: str
    worker: str
    artifact: str
    lease: str
    key: str
    content: bytes

    @property
    def path(self):
        return self.root / self.key

    @property
    def digest(self):
        return hashlib.sha256(self.content).hexdigest()

    async def expire(self, **kwargs):
        return await retention.expire_artifact(self.sessions, self.root, self.artifact,
                                               **({"retain_days": 30, "apply": True} | kwargs))

    async def snapshot(self):
        async with self.sessions() as session:
            return {model.__tablename__: (await session.execute(select(model.__table__).order_by(
                *model.__table__.primary_key.columns))).all() for model in (
                    m.WorkItem, m.WorkerHost, m.ResourceLease, m.Artifact, m.AuditRecord,
                    m.DeliveryJob, m.PreviewEndpoint, m.ConsoleLease, m.DeliveryBundle)}

    async def evidence(self):
        async with self.sessions() as session:
            return (list(await session.scalars(select(m.Artifact).order_by(m.Artifact.id))),
                    list(await session.scalars(select(m.AuditRecord).order_by(m.AuditRecord.id))))

    async def alias(self, **changes):
        async with self.sessions() as session:
            row = await session.get(m.Artifact, self.artifact)
            copy = m.Artifact(**({name: getattr(row, name) for name in (
                "work_item_id", "kind", "name", "content_type", "object_key", "size_bytes",
                "created_at", "expired_at", "purged_at", "retention_days", "retention_sha256",
            )} | changes))
            session.add(copy)
            await session.commit()
            return copy.id


async def seed(sessions, root):
    past = m.utcnow() - timedelta(days=40)
    async with sessions() as session:
        organization = m.Organization(id=f"retention-{uuid.uuid4().hex}")
        worker = m.WorkerHost(name=f"retention-{uuid.uuid4().hex}", cpu_total=4, cpu_available=4,
            memory_mb_total=8192, memory_mb_available=8192, disk_gb_available=100, active_runs=0)
        session.add_all([organization, worker])
        await session.flush()
        work = m.WorkItem(organization_id=organization.id, source=m.WorkSource.WEB,
            title="Synthetic private retention title", requirement="Preserve all live resources",
            repository="synthetic/retention", status=m.WorkStatus.COMPLETED,
            assigned_worker_id=worker.id, created_at=past, updated_at=past)
        session.add(work)
        await session.flush()
        content = b"Synthetic retention evidence\n"
        key = f"{work.id}/artifacts/{uuid.uuid4()}.txt"
        artifact = m.Artifact(work_item_id=work.id, kind="evidence", name="private-retained.txt",
            content_type="text/plain", size_bytes=len(content), object_key=key, created_at=past)
        lease = m.ResourceLease(work_item_id=work.id, worker_id=worker.id, state="released",
            token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), expires_at=past)
        session.add_all([artifact, lease])
        await session.commit()
        write_artifact_content(str(root), work.id, key, content)
        # Ordinary-artifact retention must leave delivery evidence in its separate namespace.
        bundle = root / work.id / "delivery.patch"
        bundle.write_bytes(b"Synthetic delivery evidence")
        session.add(m.DeliveryBundle(work_item_id=work.id, object_path=str(bundle),
            sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            size_bytes=bundle.stat().st_size))
        await session.commit()
        return RetentionCase(sessions, root, work.id, worker.id, artifact.id,
                             lease.id, key, content)
