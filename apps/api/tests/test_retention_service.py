"""Reviewable service defaults; Linux CI additionally checks systemd unit syntax."""

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.artifact_retention_worker import parser

ROOT = Path(__file__).resolve().parents[3]
UNIT = ROOT / "infra/systemd/kelpie-artifact-retention.service"


def fields():
    values = {}
    for line in UNIT.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    return values


def test_service_requires_policy_file_and_never_defaults_to_deletion():
    config = fields()
    assert config["EnvironmentFile"] == [
        "/etc/kelpie/api.env", "/etc/kelpie/artifact-retention.env"]
    variables = dict(value.split("=", 1) for value in config["Environment"])
    assert variables == {"ARTIFACT_RETENTION_MODE": "dry-run",
        "ARTIFACT_RETENTION_LIMIT": "100", "ARTIFACT_RETENTION_INTERVAL_SECONDS": "300"}
    assert "ARTIFACT_RETENTION_DAYS" not in variables
    variables["ARTIFACT_RETENTION_DAYS"] = "30"  # Only this owned example approves a policy.
    command = re.sub(r"\$\{([A-Z_]+)\}", lambda match: variables[match[1]], config["ExecStart"][0])
    arguments = shlex.split(command)
    assert arguments[:3] == ["/opt/kelpie/api/.venv/bin/python", "-m",
                             "app.artifact_retention_worker"]
    options = parser().parse_args(arguments[3:])
    assert options.retain_days == 30 and options.mode == "dry-run"
    assert options.limit == 100 and options.interval_seconds == 300 and not options.once
    assert options.work_id is None


def test_service_has_no_vm_privileges_or_unbounded_failure_restart():
    config = fields()
    assert config["User"] == config["Group"] == ["kelpie-api"]
    assert "SupplementaryGroups" not in config and "Requires" not in config
    assert config["ReadWritePaths"] == ["/var/lib/kelpie/artifacts"]
    assert config["ProtectSystem"] == ["strict"] and config["NoNewPrivileges"] == ["true"]
    assert config["Restart"] == ["no"] and config["TimeoutStopSec"] == ["30"]
    assert config["KillSignal"] == ["SIGTERM"] and config["KillMode"] == ["control-group"]
    assert not (ROOT / "infra/systemd/kelpie-artifact-retention.timer").exists()


def test_linux_systemd_accepts_unit_directives_without_installing_service(tmp_path):
    if sys.platform != "linux":
        pytest.skip("systemd parser is checked on Linux CI, not the macOS control host")
    analyzer = shutil.which("systemd-analyze")
    assert analyzer, "Linux CI must provide the systemd unit parser"
    # Only executable existence is adapted to CI's installed Python. No unit is installed,
    # enabled or started, and no production environment file or data is read.
    path = tmp_path / UNIT.name
    path.write_text(UNIT.read_text().replace("/opt/kelpie/api/.venv/bin/python", sys.executable))
    result = subprocess.run([analyzer, "verify", str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
