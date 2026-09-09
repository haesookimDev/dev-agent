"""Owned, ordered work used to prove progress past protected early artifacts."""

import uuid

from artifact_retention_case import seed
from sqlalchemy import select

from app import models as m


async def ordered_cases(case):
    later = await seed(case.sessions, case.root)
    async with case.sessions() as session:
        for number, item in enumerate((case, later), 1):
            row = await session.get(m.Artifact, item.artifact)
            row.id = str(uuid.UUID(int=number))
            item.artifact = row.id
        work = await session.get(m.WorkItem, case.work)
        work.status = m.WorkStatus.IMPLEMENTING
        lease = await session.get(m.ResourceLease, case.lease)
        lease.state = "active"
        await session.commit()
    return case, later


async def checkpoints(case):
    async with case.sessions() as session:
        return list(await session.scalars(select(m.ArtifactRetentionJob).order_by(
            m.ArtifactRetentionJob.scope_key)))
