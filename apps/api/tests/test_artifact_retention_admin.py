import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
import test_artifact_backup_admin as backup_tests
from test_artifact_backup import source as source
from test_artifact_backup_admin import ROOT
from test_artifact_retention import case as case

from app import artifact_retention_admin as admin
from app import models as m

backup_cli = backup_tests.cli


@pytest.fixture
def cli(backup_cli, source, tmp_path):
    _, database, _, url = backup_cli
    root, row, content = source
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        past = m.utcnow() - timedelta(days=40)
        connection.execute(sa.update(m.WorkItem).values(status=m.WorkStatus.COMPLETED,
                                                       updated_at=past))
        connection.execute(sa.update(m.Artifact).values(created_at=past))
    engine.dispose()
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
                   "DATABASE_URL": url, "ARTIFACT_ROOT": str(root)}
    def run(*args, expected=0, overrides=None):
        result = subprocess.run([sys.executable, "-m", "app.artifact_retention_admin", *args],
            cwd=tmp_path, env=environment | (overrides or {}), capture_output=True,
            text=True, timeout=15)
        assert result.returncode == expected, "retention CLI returned unexpected status"
        for private in (str(tmp_path), row.object_key, row.name, content.decode(),
                        "Synthetic private title", url):
            assert private not in result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        return result
    return run, database, row, root


def test_real_cli_defaults_dry_and_requires_explicit_apply(cli):
    run, database, row, root = cli
    before = database.read_bytes()
    result = json.loads(run("--retain-days", "30").stdout)
    assert result == {"dry_run": True, "scanned": 1, "counts": {"eligible": 1},
                      "reasons": {}, "next_cursor": None}
    assert database.read_bytes() == before
    assert (root / row.object_key).exists()
    result = json.loads(run("--retain-days", "30", "--apply").stdout)
    assert not result["dry_run"] and result["counts"] == {
        "purged": 1, "purged_aliases": 1, "bytes_removed": row.size_bytes}
    assert not (root / row.object_key).exists()
    result = json.loads(run("--retain-days", "30", "--apply").stdout)
    assert result["scanned"] == 0 and result["counts"] == {}


@pytest.mark.parametrize("args", [[], ["--retain-days", "0"], ["--retain-days", "36501"],
    ["--retain-days", "30", "--limit", "0"], ["--retain-days", "30", "--limit", "1001"],
    ["--retain-days", "30", "--work-id", "invalid"],
    ["--retain-days", "30", "--after-artifact-id", "invalid"]])
def test_invalid_cli_arguments_never_mutate_metadata_or_files(cli, args):
    run, database, row, root = cli
    before = database.read_bytes()
    assert not run(*args, "--apply", expected=2).stdout
    assert database.read_bytes() == before and (root / row.object_key).exists()


def test_cli_scope_is_explicit_and_storage_failure_is_private_nonzero(cli):
    run, _, row, root = cli
    result = json.loads(run("--retain-days", "30", "--work-id", str(uuid.uuid4()),
                            "--apply").stdout)
    assert result["scanned"] == 0 and (root / row.object_key).exists()
    (root / row.object_key).unlink()
    result = json.loads(run("--retain-days", "30", "--work-id", row.work_item_id,
                            "--apply", expected=2).stdout)
    assert result["counts"] == {"failed": 1} and result["reasons"] == {"missing_content": 1}


def test_unready_schema_and_private_configuration_fail_without_touching_files(cli):
    run, database, row, root = cli
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE alembic_version SET version_num = 'outdated'"))
    engine.dispose()
    before = database.read_bytes()
    result = run("--retain-days", "30", "--apply", expected=2)
    assert result.stderr == "artifact retention failed; private details withheld\n"
    assert database.read_bytes() == before and (root / row.object_key).exists()
    result = run("--retain-days", "30", "--apply", expected=2,
                 overrides={"DATABASE_URL": "synthetic-private-invalid-dsn"})
    assert "synthetic-private-invalid-dsn" not in result.stderr


async def test_cursor_scans_eligible_aliases_without_mutating_dry_runs(case):
    await case.alias()
    await case.alias()
    before = await case.snapshot()
    seen = []
    cursor = None
    for _ in range(3):
        result = await admin.run_batch(case.sessions, case.root, retain_days=30,
            work_id=case.work, after=cursor, limit=1)
        assert result["scanned"] == 1 and result["counts"] == {"eligible": 1}
        cursor = result["next_cursor"]
        seen.append(cursor)
    assert seen[-1] is None and len(set(seen)) == 3
    assert await case.snapshot() == before
    result = await admin.run_batch(case.sessions, case.root, retain_days=30, apply=True,
                                  work_id=case.work, limit=1)
    assert result["counts"]["purged_aliases"] == 3
    result = await admin.run_batch(case.sessions, case.root, retain_days=30, apply=True,
                                  after=result["next_cursor"], limit=1)
    assert result["scanned"] == 0 and result["next_cursor"] is None


def test_parser_never_implies_apply_or_retention_policy():
    args = admin.parser().parse_args(["--retain-days", "30"])
    assert args.retain_days == 30 and not args.apply and args.limit == 100


@pytest.mark.parametrize("invalid", [{"limit": True}, {"limit": 0}, {"limit": 1001},
    {"after": "not-a-uuid"}, {"work_id": "not-a-uuid"}, {"apply": "false"}, {"apply": 1}])
async def test_programmatic_batch_also_rejects_invalid_input_before_changes(case, invalid):
    before = await case.snapshot()
    with pytest.raises(ValueError):
        await admin.run_batch(case.sessions, case.root, retain_days=30,
                              **({"apply": True} | invalid))
    assert await case.snapshot() == before
