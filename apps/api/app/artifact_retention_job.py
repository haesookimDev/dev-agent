"""Durable bounded scan progress; deletion authority remains in artifact_retention."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .artifact_retention import canonical_id, explicit_apply, policy_days
from .artifact_retention_admin import run_batch
from .models import ArtifactRetentionJob, utcnow


def validate_options(retain_days, apply, work_id, limit):
    policy_days(retain_days)
    explicit_apply(apply)
    if work_id is not None:
        canonical_id(work_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def scope_key(root: Path, retain_days: int, apply: bool, work_id: str | None) -> str:
    value = {"format": 1, "root": str(root.resolve()), "retain_days": retain_days,
             "apply": apply, "work_id": work_id}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    scope_key: str
    cursor: str | None
    version: int


async def load_checkpoint(sessions, key: str) -> Checkpoint:
    async with asyncio.timeout(15), sessions() as session, session.begin():
        dialect = session.get_bind().dialect.name
        if dialect not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported checkpoint database")
        insert = sqlite_insert if dialect == "sqlite" else postgres_insert
        await session.execute(insert(ArtifactRetentionJob).values(scope_key=key)
                              .on_conflict_do_nothing(index_elements=["scope_key"]))
        row = (await session.execute(select(ArtifactRetentionJob.cursor,
            ArtifactRetentionJob.version).where(ArtifactRetentionJob.scope_key == key))).one()
        if row.cursor is not None:
            canonical_id(row.cursor)
        if type(row.version) is not int or row.version < 1:
            raise ValueError("invalid checkpoint version")
        return Checkpoint(key, row.cursor, row.version)


async def save_checkpoint(sessions, checkpoint: Checkpoint, cursor: str | None) -> bool:
    if cursor is not None:
        canonical_id(cursor)
    values = {"cursor": cursor, "version": checkpoint.version + 1, "updated_at": utcnow()}
    if cursor is None:
        values["sweep_completed_at"] = utcnow()
    async with asyncio.timeout(15), sessions() as session, session.begin():
        changed = await session.execute(update(ArtifactRetentionJob).where(
            ArtifactRetentionJob.scope_key == checkpoint.scope_key,
            ArtifactRetentionJob.version == checkpoint.version,
        ).values(**values))
        return changed.rowcount == 1


async def run_job(sessions, root: Path, *, retain_days: int, apply: bool = False,
                  work_id: str | None = None, limit: int = 100):
    validate_options(retain_days, apply, work_id, limit)
    checkpoint = await load_checkpoint(sessions, scope_key(root, retain_days, apply, work_id))
    batch = await run_batch(sessions, root, retain_days=retain_days, apply=apply,
                            work_id=work_id, after=checkpoint.cursor, limit=limit)
    # An interrupted/failed page must be retried. Completed deletions have their own
    # durable journal and are idempotent even if this separate checkpoint commit fails.
    saved = False
    if not batch["counts"].get("failed"):
        saved = await save_checkpoint(sessions, checkpoint, batch["next_cursor"])
    return {"job_id": checkpoint.scope_key, "checkpoint_saved": saved,
            "checkpoint_version": checkpoint.version + 1 if saved else None, "batch": batch}
