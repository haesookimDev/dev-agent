"""Disposable actual Runner process against the scoped OIDC API fixture."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from api_event_runtime import assert_no_credentials
from artifact_runtime import ROOT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from worker_authority_runtime import advance

from app.models import WorkItem


@dataclass
class RunnerRuntime:
    api: object = field(repr=False)
    process: subprocess.Popen = field(repr=False)
    directory: Path

    def work(self):
        response = self.api.clients[0].get(f"/api/work-items/{self.api.works[0]}")
        assert response.status_code == 200
        return response.json()

    def events(self, kind):
        response = self.api.clients[0].get(f"/api/work-items/{self.api.works[0]}/event-log")
        assert response.status_code == 200
        return [event for event in response.json() if event["event_type"] == kind]

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            assert self.process.poll() is None, "owned Runner exited early (log withheld)"
            time.sleep(0.05)
        raise AssertionError("owned Runner acceptance timed out (credentials withheld)")


@contextmanager
def running_runner(runtime, directory, credentials, *, fast_poll=True):
    directory.mkdir()
    source = directory / "source"
    source.mkdir()
    (source / "result.txt").write_text("broken\n")
    (source / ".kelpie.yaml").write_text(json.dumps({"verification": {"commands": [[
        sys.executable, "-c",
        "from pathlib import Path; "
        "assert Path('result.txt').read_text().startswith('fixed'), 'owned verification failure'",
    ]]}}))
    environment = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    for args in (["init", "-q"], ["add", "result.txt", ".kelpie.yaml"],
                 ["-c", "user.name=Owned Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "test: 일회용 검증 저장소 초기화"]):
        subprocess.run(["git", *args], cwd=source, env=environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

    async def configure():
        engine = create_async_engine(f"sqlite+aiosqlite:///{runtime.database}")
        try:
            async with async_sessionmaker(engine)() as session:
                work = await session.get(WorkItem, runtime.works[0])
                work.replan_limit = 0
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(configure())
    work = advance(runtime, 0, "analyzing")
    environment.update({
        "PYTHONPATH": str(ROOT / "apps/runner"), "KELPIE_CONTROL_URL": runtime.api_url,
        "KELPIE_ASSIGNMENT": base64.urlsafe_b64encode(json.dumps(work).encode()).decode(),
        "KELPIE_LEASE_TOKEN": runtime.leases[work["id"]]["X-Kelpie-Lease"],
        "KELPIE_CLONE_URL": str(source), "KELPIE_WORK_ROOT": str(directory / "runs"),
    })
    path = directory / "runner.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    process = None
    try:
        with os.fdopen(descriptor, "wb") as log:
            process = subprocess.Popen([
                sys.executable, str(Path(__file__).with_name("runner_resumption_process.py")),
                *(["--fast-poll"] if fast_poll else []),
            ], cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT)
        yield RunnerRuntime(runtime, process, directory)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        try:
            assert_no_credentials(path.read_bytes(), credentials)
        finally:
            path.unlink()
