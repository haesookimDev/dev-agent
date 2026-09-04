import asyncio
import base64
import json
import subprocess
from pathlib import Path

from kelpie_runner.main import (
    Assignment,
    create_delivery_bundle,
    evidence_files,
    redact,
    verification_commands,
)


def test_assignment_includes_correlation_id(monkeypatch) -> None:
    assignment = {
        "id": "work-id",
        "correlation_id": "33333333-3333-4333-8333-333333333333",
        "title": "Trace work",
        "requirement": "Propagate the identifier",
        "repository": "acme/service",
        "version": 2,
        "budget_minutes": 240,
        "replan_limit": 3,
    }
    monkeypatch.setenv(
        "KELPIE_ASSIGNMENT",
        base64.urlsafe_b64encode(json.dumps(assignment).encode()).decode(),
    )

    parsed = Assignment.from_environment()

    assert parsed.correlation_id == assignment["correlation_id"]


def test_redact_nested_secrets() -> None:
    payload = {"token": "secret", "nested": [{"apiKey": "secret", "safe": "value"}]}
    assert redact(payload) == {
        "token": "[REDACTED]",
        "nested": [{"apiKey": "[REDACTED]", "safe": "value"}],
    }


def test_explicit_verification_commands(tmp_path: Path) -> None:
    (tmp_path / ".kelpie.yaml").write_text(
        "verification:\n  commands:\n    - pytest -q\n    - [go, test, ./...]\n"
    )
    assert verification_commands(tmp_path) == [["pytest", "-q"], ["go", "test", "./..."]]


def test_detects_javascript_scripts_in_stable_order(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "next build", "test": "vitest", "dev": "next dev"}})
    )
    assert verification_commands(tmp_path) == [["npm", "run", "test"], ["npm", "run", "build"]]


def test_delivery_bundle_contains_tracked_and_new_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    (tmp_path / "tracked.txt").write_text("before\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("after\n")
    (tmp_path / "new.txt").write_text("new\n")

    bundle = asyncio.run(create_delivery_bundle(tmp_path))

    assert b"tracked.txt" in bundle
    assert b"new.txt" in bundle
    assert b"+after" in bundle


def test_evidence_files_reject_symlinks_and_unknown_types(tmp_path: Path) -> None:
    root = tmp_path / ".kelpie" / "artifacts"
    root.mkdir(parents=True)
    screenshot = root / "result.png"
    screenshot.write_bytes(b"png")
    (root / "program.bin").write_bytes(b"binary")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    (root / "linked.png").symlink_to(outside)

    assert evidence_files(tmp_path) == [screenshot]
