import asyncio
import os
import signal
import sys
from contextlib import suppress

import pytest

from app.delivery import run_command


async def test_delivery_command_returns_output_and_reports_failure():
    assert await run_command(sys.executable, "-c", "print('verified')") == "verified\n"
    with pytest.raises(RuntimeError, match="failed with 3: rejected"):
        await run_command(sys.executable, "-c", "print('rejected'); raise SystemExit(3)")


async def test_cancelled_delivery_command_stops_owned_process_group(tmp_path, monkeypatch):
    heartbeat = tmp_path / "child-heartbeat"
    processes = []
    isolated_groups = []
    original = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        # Isolate even the pre-fix command so regression cleanup cannot hit pytest's group.
        isolated_groups.append(kwargs.get("start_new_session", False))
        kwargs["start_new_session"] = True
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    child = (
        "import time; from pathlib import Path; "
        f"path = Path({str(heartbeat)!r}); "
        "exec('while True:\\n path.write_text(str(time.monotonic_ns()))\\n time.sleep(0.01)')"
    )
    parent = (
        f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )
    command = asyncio.create_task(run_command(sys.executable, "-c", parent))
    try:
        async with asyncio.timeout(3):
            while not heartbeat.exists():  # noqa: ASYNC110 - readiness from a separate OS process
                await asyncio.sleep(0.01)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        assert processes[0].returncode is not None
        assert isolated_groups == [True]
        stopped_at = heartbeat.read_text()
        await asyncio.sleep(0.1)
        assert heartbeat.read_text() == stopped_at
    finally:
        command.cancel()
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        await asyncio.gather(command, return_exceptions=True)
