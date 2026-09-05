import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from test_worker_credentials import database as database

from app.worker_admin import CredentialFileError, execute, parser, write_new_token


def test_admin_cli_issue_rotate_revoke_and_file_failure_are_atomic(tmp_path):
    root = Path(__file__).resolve().parents[3]
    environment = {
        "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "apps/api"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}",
    }
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(root / "apps/api/alembic.ini"),
                    "upgrade", "head"], cwd=tmp_path, env=environment, check=True,
                   capture_output=True, timeout=30)

    def command(*args, expected=0):
        completed = subprocess.run([sys.executable, "-m", "app.worker_admin", *args],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
        assert completed.returncode == expected, completed.stderr
        return completed

    first = tmp_path / "first-token"
    issued = command("issue", "--worker-name", "cli-worker", "--reason", "test provisioning",
                     "--output", str(first))
    token = first.read_text().strip()
    identity = json.loads(issued.stdout)
    assert token not in issued.stdout + issued.stderr
    assert first.stat().st_mode & 0o777 == 0o600
    failed = command("issue", "--worker-name", "failed-worker", "--reason", "test failure",
                     "--output", str(first), expected=2)
    assert first.read_text().strip() == token
    assert str(first) not in failed.stderr
    second = tmp_path / "second-token"
    rotated = command("rotate", "--credential-id", identity["credential_id"], "--reason",
                      "test rotation", "--output", str(second), "--overlap-seconds", "120")
    replacement = json.loads(rotated.stdout)
    assert replacement["worker_id"] == identity["worker_id"]
    command("revoke", "--credential-id", identity["credential_id"], "--reason", "test revocation")
    listed = command("list")
    metadata = json.loads(listed.stdout)
    assert len(metadata) == 2
    assert metadata[0]["revoked_at"] is not None
    assert metadata[1]["revoked_at"] is None
    assert token not in listed.stdout + listed.stderr
    assert second.read_text().strip() not in listed.stdout + listed.stderr
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'admin.db'}")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM worker_hosts")).scalar_one() == 1
        assert connection.execute(sa.text(
            "SELECT count(*) FROM worker_credential_events"
        )).scalar_one() == 3
    engine.dispose()
    retained = (tmp_path / "admin.db").read_bytes()
    assert token.encode() not in retained
    assert second.read_text().strip().encode() not in retained


def test_admin_cli_quarantine_is_idempotent_and_cannot_reissue_credentials(tmp_path):
    root = Path(__file__).resolve().parents[3]
    environment = {
        "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "apps/api"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'quarantine.db'}",
    }
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(root / "apps/api/alembic.ini"),
                    "upgrade", "head"], cwd=tmp_path, env=environment, check=True,
                   capture_output=True, timeout=30)

    def command(*args, expected=0):
        completed = subprocess.run([sys.executable, "-m", "app.worker_admin", *args],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
        assert completed.returncode == expected, completed.stderr
        return completed

    token_file = tmp_path / "token"
    issued = command("issue", "--worker-name", "quarantine-worker", "--reason", "test setup",
                     "--output", str(token_file))
    identity = json.loads(issued.stdout)
    token = token_file.read_text().strip()
    arguments = ("quarantine", "--worker-id", identity["worker_id"], "--reason", "test incident")
    isolated = command(*arguments)
    result = json.loads(isolated.stdout)
    assert result == {
        "worker_id": identity["worker_id"], "already_quarantined": False,
        "revoked_credentials": 1, "invalidated_leases": 0, "affected_work_ids": [],
        "physical_cleanup_required": True,
    }
    repeated = command(*arguments)
    assert json.loads(repeated.stdout)["already_quarantined"] is True
    listed = command("list")
    assert json.loads(listed.stdout)[0]["revoked_at"] is not None
    blocked = command("issue", "--worker-name", "quarantine-worker", "--reason", "test reissue",
                      "--output", str(tmp_path / "must-not-exist"), expected=2)
    assert "quarantined workers" in blocked.stderr
    assert not (tmp_path / "must-not-exist").exists()
    assert token not in isolated.stdout + isolated.stderr + repeated.stdout + listed.stdout
    assert token_file.read_text().strip() == token  # Operator owns physical token-file cleanup.
    unknown = command("quarantine", "--worker-id", "unknown", "--reason", "test", expected=2)
    assert "worker not found" in unknown.stderr
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'quarantine.db'}")
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT count(*) FROM worker_credential_events WHERE action = 'quarantined'"
        )).scalar_one() == 1
    engine.dispose()


def test_token_file_never_overwrites_or_follows_existing_symlink(tmp_path):
    existing = tmp_path / "existing"
    existing.write_text("keep-existing-content")
    link = tmp_path / "link"
    link.symlink_to(existing)
    for path in (existing, link):
        with pytest.raises(CredentialFileError):
            write_new_token(path, "synthetic-token")
    assert existing.read_text() == "keep-existing-content"
    assert link.is_symlink()


def test_token_file_write_failure_removes_only_its_own_file(tmp_path, monkeypatch):
    def fail_sync(_):
        raise OSError("synthetic-write-error")

    monkeypatch.setattr("app.worker_admin.os.fsync", fail_sync)
    path = tmp_path / "new-token"
    with pytest.raises(CredentialFileError, match="could not persist"):
        write_new_token(path, "synthetic-token")
    assert not path.exists()


async def test_commit_failure_removes_uncommitted_token_file(database, tmp_path, monkeypatch):
    async def fail_commit():
        raise RuntimeError("synthetic-commit-failure")

    monkeypatch.setattr(database, "commit", fail_commit)
    path = tmp_path / "new-token"
    arguments = parser().parse_args(["issue", "--worker-name", "worker-a", "--reason", "test",
                                     "--output", str(path)])
    with pytest.raises(RuntimeError, match="synthetic-commit-failure"):
        await execute(database, arguments)
    assert not path.exists()
    await database.rollback()
    count = await database.execute(sa.text("SELECT count(*) FROM worker_credentials"))
    assert count.scalar() == 0
