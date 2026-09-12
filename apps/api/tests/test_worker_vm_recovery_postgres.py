"""Opt-in real Worker process/KVM recovery through loopback TCP and PostgreSQL."""

import asyncio
import json
import os
import re
import socket
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select
from test_api import create_work
from test_worker_lease_reconciliation import snapshot
from test_worker_lease_reconciliation_postgres import client as client

from app import main
from app.db import get_session
from app.models import ResourceLease, WorkItem, WorkStatus

__all__ = ["client"]
LAB_CONFIG = os.environ.get("KELPIE_TEST_LIBVIRT_RECOVERY_CONFIG")
pytestmark = pytest.mark.skipif(
    not LAB_CONFIG or not os.environ.get("KELPIE_TEST_POSTGRES_URL"),
    reason="explicit disposable libvirt fixture and dedicated PostgreSQL URL required",
)


async def stop_owned_process(process):
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        process.kill()
        await process.wait()


async def test_real_worker_process_recovers_owned_vm_without_run_token(
    client, worker_headers,
):
    lab = json.loads(await asyncio.to_thread(Path(LAB_CONFIG).read_text))
    assert lab["acknowledgement"] == "disposable-host-and-isolated-api-only"
    assert Path(lab["ssh_config"]).is_absolute()
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", lab["ssh_host"])
    assert re.fullmatch(r"/home/kelpie/worker-recovery-[0-9]+\.test", lab["test_binary"])
    assert re.fullmatch(r"/home/kelpie/worker-recovery-[0-9]+", lab["worker_binary"])
    assert isinstance(lab["forward_port"], int) and 1024 <= lab["forward_port"] <= 65535
    ssh = ["ssh", "-F", lab["ssh_config"], "-S", "none", "-o", "ControlMaster=no",
           "-o", "ControlPersist=no", "-o", "ForwardAgent=no", "-o", "BatchMode=yes"]
    resolved = await asyncio.create_subprocess_exec(
        *ssh, "-G", lab["ssh_host"], stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    configuration, _ = await asyncio.wait_for(resolved.communicate(), 5)
    assert resolved.returncode == 0
    options = dict(line.split(" ", 1) for line in configuration.decode().splitlines())
    assert options["hostname"] == "127.0.0.1" and options["user"] == "kelpie"

    work = await create_work(client, "Isolated real Worker restart recovery")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        main.app, log_config=None, access_log=False, lifespan="off", log_level="critical",
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    forwarder = runner = health = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("isolated API exited before startup")
                await asyncio.sleep(0.01)
        forwarder = await asyncio.create_subprocess_exec(
            *ssh, "-o", "ExitOnForwardFailure=yes", "-N", "-R",
            f"127.0.0.1:{lab['forward_port']}:127.0.0.1:{port}", lab["ssh_host"],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        # Probe only health before sending credentials; the owned SSH process
        # must still be alive, so a preexisting listener cannot masquerade as it.
        health = await asyncio.create_subprocess_exec(
            *ssh, lab["ssh_host"], "curl", "--retry", "5", "--retry-connrefused",
            "--retry-max-time", "8", "--max-time", "1", "--silent", "--fail",
            f"http://127.0.0.1:{lab['forward_port']}/healthz",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        assert await asyncio.wait_for(health.wait(), 12) == 0
        assert forwarder.returncode is None
        runner = await asyncio.create_subprocess_exec(
            *ssh, lab["ssh_host"], "sudo", "-n", "-u", "kelpie", "env",
            "KELPIE_LIBVIRT_TEST_ACK=disposable-host-only",
            "KELPIE_LIBVIRT_API_TEST_ACK=isolated-api-only", lab["test_binary"],
            "-test.run", "^TestDedicatedLibvirtAPIRecovery$", "-test.v",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        credential = worker_headers["Authorization"].removeprefix("Bearer ")
        payload = json.dumps({
            "control_url": f"http://127.0.0.1:{lab['forward_port']}", "worker_name": "worker-one",
            "credential": credential, "work_id": work["id"], "worker_binary": lab["worker_binary"],
        }).encode()
        output, _ = await asyncio.wait_for(runner.communicate(payload), 270)
        # Never emit the generated credential, including on an unexpected failure.
        print(output.decode(errors="replace").replace(credential, "[redacted]"))
        assert credential.encode() not in output, "fixture output exposed its generated credential"
        assert runner.returncode == 0, "real Worker/libvirt recovery failed; journal retained"
        assert b"real networkless KVM fixture -> new Worker process" in output
        async for session in main.app.dependency_overrides[get_session]():
            lease = await session.scalar(select(ResourceLease).where(
                ResourceLease.work_item_id == work["id"],
            ))
            item = await session.get(WorkItem, work["id"])
            assert lease is not None and item.status == WorkStatus.FAILED and item.version == 3
            worker_id, lease_id = lease.worker_id, lease.id
        assert await snapshot(worker_id, lease_id) == (4, 8192, 60, 0, "released", 1, 1)
        print("PostgreSQL verified: full capacity, zero active runs, "
              "one release event and one reconciliation audit")
    finally:
        await stop_owned_process(runner)
        await stop_owned_process(health)
        await stop_owned_process(forwarder)
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serving), 3)
        finally:
            if not serving.done():
                serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            listener.close()
