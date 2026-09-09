"""Real scheduler processes, migrations, files, signals and restart checkpoints."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic import command
from artifact_retention_case import seed
from retention_job_case import checkpoints, ordered_cases
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_migrations import migration_config

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def worker(tmp_path):
    database = tmp_path / "scheduled.db"
    url = f"sqlite+aiosqlite:///{database}"
    await asyncio.to_thread(command.upgrade, migration_config(url), "head")
    engine = create_async_engine(url)
    try:
        first = await seed(async_sessionmaker(engine, expire_on_commit=False), tmp_path / "files")
        first, later = await ordered_cases(first)
        environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
                       "DATABASE_URL": url, "ARTIFACT_ROOT": str(first.root)}

        def private(output):
            for value in (str(tmp_path), url, first.key, later.key, first.content.decode(),
                          "Synthetic private retention title", "private-retained.txt"):
                assert value not in output
            assert "Traceback" not in output

        @asynccontextmanager
        async def process(*args, overrides=None):
            child = await asyncio.create_subprocess_exec(sys.executable, "-m",
                "app.artifact_retention_worker", *args, cwd=tmp_path,
                env=environment | (overrides or {}), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                yield child
            finally:
                if child.returncode is None:
                    child.terminate()
                try:
                    out, err = await asyncio.wait_for(child.communicate(), 5)
                except TimeoutError:
                    child.kill()
                    out, err = await child.communicate()
                    pytest.fail("owned retention process did not stop on SIGTERM")
                private((out + err).decode())

        async def run(*args, expected=0, overrides=None):
            async with process(*args, overrides=overrides) as child:
                out, err = await asyncio.wait_for(child.communicate(), 15)
                private((out + err).decode())
                assert child.returncode == expected, "unexpected scheduler exit status"
                return out.decode(), err.decode()

        yield first, later, run, process, private, database
    finally:
        await engine.dispose()


APPLY = ("--retain-days", "30", "--mode", "apply", "--limit", "1", "--once")


async def test_separate_processes_resume_past_live_work_without_duplicate_deletion(worker):
    first, later, run, _, _, _ = worker
    before = await first.snapshot()
    one = json.loads((await run(*APPLY))[0])
    assert one["batch"]["counts"] == {"protected": 1}
    assert (await checkpoints(first))[0].cursor == first.artifact
    assert first.path.exists() and later.path.exists()
    two = json.loads((await run(*APPLY))[0])
    assert two["checkpoint_version"] == 3 and two["batch"]["counts"]["purged"] == 1
    assert first.path.exists() and not later.path.exists()
    three = json.loads((await run(*APPLY))[0])
    assert three["checkpoint_version"] == 4 and three["batch"]["counts"] == {"protected": 1}
    after = await first.snapshot()
    assert len(after["audit_records"]) == 2
    for table in before.keys() - {"artifacts", "audit_records"}:
        assert after[table] == before[table]


async def test_default_dry_run_persists_only_scan_progress(worker):
    first, later, run, _, _, _ = worker
    before = await first.snapshot()
    result = json.loads((await run("--retain-days", "30", "--once"))[0])
    assert result["checkpoint_saved"] and result["batch"]["dry_run"]
    assert result["batch"]["counts"] == {"protected": 1, "eligible": 1}
    assert await first.snapshot() == before
    assert len(await checkpoints(first)) == 1 and first.path.exists() and later.path.exists()


async def test_periodic_process_advances_then_stops_during_interval(worker):
    first, later, _, process, private, _ = worker
    async with process("--retain-days", "30", "--mode", "apply", "--limit", "1",
                       "--interval-seconds", "1") as child:
        for version in (2, 3):
            line = (await asyncio.wait_for(child.stdout.readline(), 15)).decode()
            private(line)
            assert json.loads(line)["checkpoint_version"] == version
        child.terminate()
        out, err = await asyncio.wait_for(child.communicate(), 5)
        private((out + err).decode())
        assert child.returncode == 0 and not out and not err
    assert first.path.exists() and not later.path.exists()
    assert (await checkpoints(first))[0].version == 3


async def test_signal_interrupts_long_sleep_and_restart_uses_saved_cursor(worker):
    first, later, run, process, private, _ = worker
    async with process("--retain-days", "30", "--mode", "apply", "--limit", "1",
                       "--interval-seconds", "86400") as child:
        line = (await asyncio.wait_for(child.stdout.readline(), 15)).decode()
        private(line)
        assert json.loads(line)["checkpoint_version"] == 2
        child.terminate()
        out, err = await asyncio.wait_for(child.communicate(), 5)
        private((out + err).decode())
        assert child.returncode == 0 and not out and not err
    assert first.path.exists() and later.path.exists()
    assert json.loads((await run(*APPLY))[0])["batch"]["counts"]["purged"] == 1


@pytest.mark.parametrize("args", [(), ("--retain-days", "0"),
    ("--retain-days", "30", "--limit", "1001"),
    ("--retain-days", "30", "--work-id", "invalid"),
    ("--retain-days", "30", "--interval-seconds", "0")])
async def test_invalid_arguments_do_not_write_checkpoint_or_artifacts(worker, args):
    first, later, run, _, _, database = worker
    before = database.read_bytes()
    out, _ = await run(*args, "--once", expected=2)
    assert not out and database.read_bytes() == before
    assert not await checkpoints(first) and first.path.exists() and later.path.exists()


async def test_failed_batch_exits_nonzero_with_unadvanced_checkpoint(worker):
    first, later, run, _, _, _ = worker
    later.path.unlink()
    out, err = await run("--retain-days", "30", "--mode", "apply", "--once", expected=2)
    result = json.loads(out)
    assert not result["checkpoint_saved"] and result["batch"]["counts"]["failed"] == 1
    assert (await checkpoints(first))[0].version == 1 and first.path.exists()
    assert err == "artifact retention batch incomplete; investigate before retry\n"


async def test_private_configuration_failure_is_sanitized(worker):
    first, later, run, _, _, database = worker
    before = database.read_bytes()
    out, err = await run(*APPLY, expected=2,
                        overrides={"DATABASE_URL": "synthetic-private-invalid-dsn"})
    assert not out and err == "artifact retention worker failed; private details withheld\n"
    assert database.read_bytes() == before and first.path.exists() and later.path.exists()
