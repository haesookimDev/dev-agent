import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def jobs():
    path = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
    return yaml.safe_load(path.read_text())["jobs"]


def test_every_api_partition_is_required_without_expanding_version_matrix(jobs):
    api = jobs.get("api")
    assert api is not None, "API checks must be split before the eight-minute job limit"
    assert api["strategy"] == {"fail-fast": False, "matrix": {"partition": [1, 2]}}
    assert api["timeout-minutes"] == 8
    assert not api.get("if") and not api.get("continue-on-error")
    step = next(step for step in api["steps"] if step.get("name") == "Test API partition")
    assert step["run"] == "make test-api PYTHON=python"
    assert step["env"] == {
        "PYTEST_ADDOPTS": "--api-partition=${{ matrix.partition }} --durations=10"}
    assert not step.get("if") and not step.get("continue-on-error")


def test_existing_python_gate_waits_and_cannot_skip_failed_dependencies(jobs):
    python = jobs["python"]
    assert python["name"] == "Python"
    assert python["needs"] == ["api"]
    assert python["if"] == "${{ always() }}"
    assert not python.get("continue-on-error")
    gate = python["steps"][0]
    assert gate["name"] == "Require every API partition"
    assert gate["env"] == {"API_RESULT": "${{ needs.api.result }}"}
    assert not gate.get("if") and not gate.get("continue-on-error")
    assert "make test-api" not in "\n".join(step.get("run", "") for step in python["steps"])


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_actual_gate_shell_accepts_only_success(jobs, result):
    gate = jobs["python"]["steps"][0]
    assert gate.get("name") == "Require every API partition"
    completed = subprocess.run(
        ["bash", "-e", "-c", gate["run"]], env=dict(os.environ, API_RESULT=result),
        capture_output=True, text=True, timeout=5,
    )
    assert (completed.returncode == 0) is (result == "success")
