import asyncio
import uuid

import pytest
from retention_job_case import checkpoints, ordered_cases
from sqlalchemy import update
from test_artifact_retention import case as case

from app import artifact_retention_job as job
from app import models as m


async def test_checkpoint_moves_past_active_work_and_starts_another_sweep(case):
    first, later = await ordered_cases(case)
    before = await case.snapshot()
    result = await job.run_job(case.sessions, case.root, retain_days=30, apply=True, limit=1)
    assert result["checkpoint_saved"] and result["checkpoint_version"] == 2
    assert result["batch"]["reasons"] == {"lease_not_released": 1}
    assert result["batch"]["next_cursor"] == first.artifact
    assert first.path.exists() and later.path.exists()
    result = await job.run_job(case.sessions, case.root, retain_days=30, apply=True, limit=1)
    assert result["checkpoint_saved"] and result["checkpoint_version"] == 3
    assert result["batch"]["counts"]["purged"] == 1
    assert result["batch"]["next_cursor"] is None
    assert first.path.exists() and not later.path.exists()
    state = (await checkpoints(case))[0]
    assert state.cursor is None and state.sweep_completed_at is not None
    result = await job.run_job(case.sessions, case.root, retain_days=30, apply=True, limit=100)
    assert result["batch"]["scanned"] == 1
    assert result["batch"]["counts"] == {"protected": 1}
    after = await case.snapshot()
    assert {k: v for k, v in after.items() if k not in {"artifacts", "audit_records"}} == {
        k: v for k, v in before.items() if k not in {"artifacts", "audit_records"}}
    assert len(after["audit_records"]) == 2


async def test_dry_run_only_changes_job_progress_and_cannot_skip_apply_candidates(case):
    await case.alias()
    before = await case.snapshot()
    dry = await job.run_job(case.sessions, case.root, retain_days=30, limit=1)
    assert dry["checkpoint_saved"] and dry["batch"]["counts"] == {"eligible": 1}
    assert await case.snapshot() == before and case.path.read_bytes() == case.content
    applied = await job.run_job(case.sessions, case.root, retain_days=30, apply=True, limit=1)
    assert dry["job_id"] != applied["job_id"]
    assert applied["batch"]["counts"]["purged_aliases"] == 2
    assert len(await checkpoints(case)) == 2
    assert not case.path.exists()


def test_storage_work_policy_and_mode_have_separate_progress(tmp_path):
    key = job.scope_key(tmp_path, 30, True, None)
    assert len({key, job.scope_key(tmp_path / "other", 30, True, None),
                job.scope_key(tmp_path, 31, True, None),
                job.scope_key(tmp_path, 30, False, None),
                job.scope_key(tmp_path, 30, True, str(uuid.uuid4()))}) == 5
    assert str(tmp_path) not in key
    assert job.scope_key(tmp_path / "sub" / "..", 30, True, None) == key


@pytest.mark.parametrize("invalid", [
    {"retain_days": 0}, {"retain_days": True}, {"retain_days": 36501},
    {"apply": "false"}, {"work_id": "private-invalid"}, {"limit": True}, {"limit": 1001},
])
async def test_invalid_job_options_never_create_progress_or_change_artifacts(case, invalid):
    before = await case.snapshot()
    with pytest.raises(ValueError):
        await job.run_job(case.sessions, case.root, **({"retain_days": 30} | invalid))
    assert await case.snapshot() == before and not await checkpoints(case)
    assert case.path.read_bytes() == case.content


async def test_failed_batch_keeps_checkpoint_for_repair_and_retry(case):
    case.path.unlink()
    result = await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert result["batch"]["counts"] == {"failed": 1}
    assert not result["checkpoint_saved"] and result["checkpoint_version"] is None
    state = (await checkpoints(case))[0]
    assert state.version == 1 and state.cursor is state.sweep_completed_at is None
    case.path.write_bytes(case.content)
    retried = await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert retried["checkpoint_saved"] and retried["batch"]["counts"]["purged"] == 1
    assert not case.path.exists()


async def test_crash_after_deletion_retries_without_duplicate_audits(case, monkeypatch):
    original = job.save_checkpoint
    async def crash(*args):
        raise RuntimeError("synthetic checkpoint failure")
    monkeypatch.setattr(job, "save_checkpoint", crash)
    with pytest.raises(RuntimeError, match="synthetic checkpoint failure"):
        await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert not case.path.exists()
    assert (await checkpoints(case))[0].version == 1
    assert len((await case.evidence())[1]) == 2
    monkeypatch.setattr(job, "save_checkpoint", original)
    retried = await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert retried["checkpoint_saved"] and retried["batch"]["scanned"] == 0
    assert len((await case.evidence())[1]) == 2


async def test_stale_checkpoint_writer_cannot_replace_newer_progress(case):
    key = job.scope_key(case.root, 30, True, None)
    before = await job.load_checkpoint(case.sessions, key)
    assert await job.save_checkpoint(case.sessions, before, str(uuid.UUID(int=2)))
    assert not await job.save_checkpoint(case.sessions, before, str(uuid.UUID(int=1)))
    after = await job.load_checkpoint(case.sessions, key)
    assert after.version == 2 and after.cursor == str(uuid.UUID(int=2))


async def test_concurrent_jobs_journal_once_and_advance_one_version(case, monkeypatch):
    original, ready = job.run_batch, asyncio.Event()
    arrivals = 0
    async def together(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        return await original(*args, **kwargs)
    monkeypatch.setattr(job, "run_batch", together)
    results = await asyncio.gather(*(job.run_job(case.sessions, case.root,
        retain_days=30, apply=True) for _ in range(2)))
    assert sorted(result["checkpoint_saved"] for result in results) == [False, True]
    assert (await checkpoints(case))[0].version == 2
    assert len((await case.evidence())[1]) == 2 and not case.path.exists()


async def test_corrupt_retained_cursor_fails_before_deletion(case):
    key = job.scope_key(case.root, 30, True, None)
    await job.load_checkpoint(case.sessions, key)
    async with case.sessions() as session:
        await session.execute(update(m.ArtifactRetentionJob).values(cursor="x" * 36))
        await session.commit()
    before = await case.snapshot()
    with pytest.raises(ValueError):
        await job.run_job(case.sessions, case.root, retain_days=30, apply=True)
    assert await case.snapshot() == before and case.path.exists()
