import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kelpie_runner import main
from kelpie_runner.main import Assignment, clone_repository, terminate_process_group


def assignment() -> Assignment:
    return Assignment(
        "owned-work", "trace", "Fixture", "Clone safely", "example/fixture", 2, 60, 0,
    )


def test_clone_has_bounded_default_and_clones_repository(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    (source / "README.md").write_text("fixture\n")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
    monkeypatch.setenv("KELPIE_CLONE_URL", str(source))
    root = tmp_path / "work"
    root.mkdir()

    repository = asyncio.run(clone_repository(assignment(), root))

    assert inspect.signature(clone_repository).parameters["timeout_seconds"].default == 120
    assert (repository / "README.md").read_text() == "fixture\n"


def test_clone_failure_does_not_expose_stderr_or_url_credentials(monkeypatch, tmp_path):
    install_fake_git(monkeypatch, tmp_path, "failure")
    secret = "synthetic-clone-credential"
    monkeypatch.setenv("KELPIE_CLONE_URL", f"https://token:{secret}@example.invalid/repository.git")

    with pytest.raises(RuntimeError) as failure:
        asyncio.run(clone_repository(assignment(), tmp_path, timeout_seconds=2))

    assert str(failure.value) == "git clone failed with exit code 17"
    assert secret not in str(failure.value)


def test_clone_discards_subprocess_output_instead_of_buffering(monkeypatch, tmp_path):
    captured = {}

    class Process:
        pid = 12345
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def create_subprocess(*_args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "terminate_process_group", cleanup)

    repository = asyncio.run(clone_repository(assignment(), tmp_path))

    assert repository == tmp_path / "repository"
    assert captured["stdout"] is asyncio.subprocess.DEVNULL
    assert captured["stderr"] is asyncio.subprocess.DEVNULL
    cleanup.assert_awaited_once()


@pytest.mark.parametrize("exit_code", [0, 17])
def test_clone_cleans_owned_group_even_after_parent_has_exited(monkeypatch, tmp_path, exit_code):
    class Process:
        pid = 12345
        returncode = exit_code

        async def wait(self):
            return exit_code

    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    cleanup = AsyncMock()
    monkeypatch.setattr(main, "terminate_process_group", cleanup)
    if exit_code:
        with pytest.raises(RuntimeError, match="git clone failed with exit code 17"):
            asyncio.run(clone_repository(assignment(), tmp_path))
    else:
        asyncio.run(clone_repository(assignment(), tmp_path))
    cleanup.assert_awaited_once_with(process)


def test_termination_kills_term_ignoring_group_after_parent_exits(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    parent = tmp_path / "parent.py"
    parent.write_text(
        """import os
import subprocess
import sys
import time
from pathlib import Path

child_code = '''import os
import signal
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["KELPIE_TEST_CHILD_PID_FILE"]).write_text(str(os.getpid()))
while True:
    time.sleep(1)
'''
subprocess.Popen(
    [sys.executable, "-c", child_code],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
marker = Path(os.environ["KELPIE_TEST_CHILD_PID_FILE"])
for _ in range(200):
    if marker.exists():
        break
    time.sleep(0.01)
raise SystemExit(0 if marker.exists() else 2)
"""
    )

    async def scenario():
        environment = os.environ | {"KELPIE_TEST_CHILD_PID_FILE": str(child_pid_file)}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(parent),
            env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        process_group = process.pid
        try:
            assert await asyncio.wait_for(process.wait(), timeout=3) == 0
            child_pid = int(child_pid_file.read_text())
            os.kill(child_pid, 0)
            await terminate_process_group(process, terminate_timeout=0.1, kill_timeout=3)
            result = await asyncio.to_thread(
                subprocess.run,
                ["ps", "-o", "state=", "-p", str(child_pid)],
                check=False,
                capture_output=True,
                text=True,
            )
            state = result.stdout.strip()
            assert not state or state.startswith("Z")
        finally:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
def test_clone_timeout_or_cancellation_joins_entire_process_group(
    monkeypatch, tmp_path, outcome,
):
    pid_file = tmp_path / "clone.pid"
    install_fake_git(monkeypatch, tmp_path, "wait")
    monkeypatch.setenv("KELPIE_TEST_CLONE_PID_FILE", str(pid_file))

    async def scenario():
        timeout_seconds = 1 if outcome == "timeout" else 30
        task = asyncio.create_task(
            clone_repository(assignment(), tmp_path, timeout_seconds=timeout_seconds)
        )
        assert await asyncio.to_thread(wait_for_path, pid_file)
        process_group = int(pid_file.read_text().splitlines()[0])
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="git clone timed out after 1 seconds"):
                await task
        with pytest.raises(ProcessLookupError):
            os.killpg(process_group, 0)

    asyncio.run(scenario())


def wait_for_path(path: Path, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))
    return True


def install_fake_git(monkeypatch, tmp_path: Path, behavior: str) -> None:
    executable = tmp_path / "git"
    if behavior == "failure":
        source = """#!/usr/bin/env python3
import sys
print(sys.argv[2], file=sys.stderr)
raise SystemExit(17)
"""
    else:
        source = """#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(os.environ["KELPIE_TEST_CLONE_PID_FILE"], "w") as marker:
    marker.write(f"{os.getpid()}\\n{child.pid}\\n")

def stop(_signum, _frame):
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)
    raise SystemExit(143)

signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(1)
"""
    executable.write_text(source)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
