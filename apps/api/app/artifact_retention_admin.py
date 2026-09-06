"""Bounded, explicit control-host retention pass; no service/VM/resource lifecycle changes."""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from .artifact_retention import canonical_id, expire_artifact, explicit_apply, policy_days
from .models import Artifact


async def run_batch(sessions, root: Path, *, retain_days: int, apply: bool = False,
                    work_id: str | None = None, after: str | None = None, limit: int = 100):
    policy_days(retain_days)
    explicit_apply(apply)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    query = select(Artifact.id).where(Artifact.purged_at.is_(None)).order_by(Artifact.id)
    if work_id is not None:
        query = query.where(Artifact.work_item_id == canonical_id(work_id))
    if after is not None:
        query = query.where(Artifact.id > canonical_id(after))
    async with asyncio.timeout(15), sessions() as session:
        identities = list(await session.scalars(query.limit(limit + 1)))
    totals, reasons = Counter(), Counter()
    for identity in identities[:limit]:
        result = await expire_artifact(sessions, root, identity,
                                       retain_days=retain_days, apply=apply)
        totals[result.status] += 1
        if result.status == "purged":
            totals["purged_aliases"] += result.aliases
            totals["bytes_removed"] += result.bytes_removed
        if result.reason:
            reasons[result.reason] += 1
    return {"dry_run": not apply, "scanned": min(len(identities), limit),
            "counts": dict(totals), "reasons": dict(reasons),
            "next_cursor": identities[limit - 1] if len(identities) > limit else None}


async def execute(arguments):
    from .config import get_settings
    from .db import SessionLocal, engine, get_schema_readiness

    try:
        if not (await get_schema_readiness()).ready:
            raise ValueError("database schema must match the deployed API")
        return await run_batch(SessionLocal, Path(get_settings().artifact_root),
            retain_days=arguments.retain_days, apply=arguments.apply, work_id=arguments.work_id,
            after=arguments.after_artifact_id, limit=arguments.limit)
    finally:
        await engine.dispose()


def parser():
    result = argparse.ArgumentParser(description="Expire ordinary control-plane artifacts. "
        "Uses DATABASE_URL and ARTIFACT_ROOT; defaults to a dry run. Never deletes VM disks.")
    result.add_argument("--retain-days", type=int, required=True,
                        help="explicit policy, 1..36500 days; no automatic default")
    result.add_argument("--apply", action="store_true", help="persist expiration and remove files")
    result.add_argument("--work-id", help="restrict this pass to one canonical work UUID")
    result.add_argument("--after-artifact-id", help="resume scanning after the reported cursor")
    result.add_argument("--limit", type=int, default=100, help="metadata candidates, 1..1000")
    return result


def main():
    command_parser = parser()
    arguments = command_parser.parse_args()
    try:
        result = asyncio.run(execute(arguments))
    except Exception:
        command_parser.exit(2, "artifact retention failed; private details withheld\n")
    print(json.dumps(result, sort_keys=True))
    if result["counts"].get("failed"):
        command_parser.exit(2, "some artifacts could not be processed; retry after investigation\n")


if __name__ == "__main__":
    main()
