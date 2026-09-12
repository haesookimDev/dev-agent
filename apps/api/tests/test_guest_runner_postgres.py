"""Opt-in real guest Runner bootstrap against TLS API and PostgreSQL."""

import asyncio
import base64
import json
import os
import re
import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from httpx import AsyncClient
from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send
from test_api import create_work
from test_worker_lease_reconciliation_postgres import client as client
from test_worker_vm_recovery_postgres import stop_owned_process

from app import main
from app.db import get_session
from app.models import AgentEvent, ResourceLease, WorkItem, WorkStatus

__all__ = ["client"]
LAB_CONFIG = os.environ.get("KELPIE_TEST_LIBVIRT_RECOVERY_CONFIG")
RUNNER_SOURCE = Path(__file__).resolve().parents[2] / "runner/kelpie_runner/main.py"
SCENARIOS = ("trusted", "untrusted-ca", "wrong-hostname", "cancelled")
CLEAN_ENV = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
pytestmark = pytest.mark.skipif(
    not LAB_CONFIG or not os.environ.get("KELPIE_TEST_POSTGRES_URL"),
    reason="explicit disposable libvirt fixture and dedicated PostgreSQL URL required",
)


@dataclass
class TLSObservation:
    """Record metadata only after a TLS handshake reaches the real ASGI app."""

    app: ASGIApp
    work_id: str
    scenario: str
    client: AsyncClient
    requests: int = 0
    run_requests: int = 0
    lease_requests: int = 0
    cancellations: int = 0
    responses: list[tuple[str, str, int]] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.requests += 1
        if any(name.lower() == b"x-kelpie-lease" for name, _ in scope["headers"]):
            self.lease_requests += 1
        method = scope["method"]
        path = scope["path"]
        if path.startswith(f"/api/runs/{self.work_id}"):
            self.run_requests += 1
        request_body = bytearray()
        body_too_large = False

        async def observe_receive():
            nonlocal body_too_large
            message = await receive()
            if message["type"] == "http.request" and not body_too_large:
                body = message.get("body", b"")
                if len(request_body) + len(body) <= 4096:
                    request_body.extend(body)
                else:
                    request_body.clear()
                    body_too_large = True
            return message

        async def observe_response(message) -> None:
            if message["type"] == "http.response.start" and path.startswith(
                f"/api/runs/{self.work_id}"
            ):
                self.responses.append((method, path, message["status"]))
                try:
                    payload = json.loads(request_body) if not body_too_large else None
                except (TypeError, ValueError):
                    payload = None
                if (
                    self.scenario == "cancelled"
                    and method == "POST"
                    and path == f"/api/runs/{self.work_id}/transition"
                    and message["status"] == 200
                    and self.cancellations == 0
                    and payload
                    == {
                        "status": "analyzing",
                        "expected_version": 2,
                        "message": "Runner control bootstrap completed",
                        "payload": {},
                    }
                ):
                    # The bootstrap transaction is committed before response
                    # delivery. Use the same fresh lease authority for a real,
                    # separate API transition before the guest receives v3.
                    lease = next(
                        value
                        for name, value in scope["headers"]
                        if name.lower() == b"x-kelpie-lease"
                    )
                    cancelled = await self.client.post(
                        path,
                        headers={"X-Kelpie-Lease": lease.decode("ascii")},
                        json={
                            "status": "cancelled",
                            "expected_version": 3,
                            "message": "Authenticated execution cancellation after bootstrap",
                            "payload": {},
                        },
                    )
                    assert cancelled.status_code == 200
                    state = cancelled.json()
                    assert state["id"] == self.work_id
                    assert state["status"] == "cancelled" and state["version"] == 4
                    self.cancellations += 1
            await send(message)

        await self.app(scope, observe_receive, observe_response)


def create_listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    return listener


async def start_server(
    app: ASGIApp,
    listener: socket.socket,
    *,
    certificate: Path | None = None,
    private_key: Path | None = None,
) -> tuple[uvicorn.Server, asyncio.Task]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            log_level="critical",
            ssl_certfile=str(certificate) if certificate else None,
            ssl_keyfile=str(private_key) if private_key else None,
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("owned API exited before startup")
                await asyncio.sleep(0.01)
    except BaseException:
        server.should_exit = True
        if not serving.done():
            serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        raise
    return server, serving


async def stop_servers(
    servers: list[uvicorn.Server], tasks: list[asyncio.Task], listeners: list[socket.socket]
) -> None:
    for server in servers:
        server.should_exit = True
    try:
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for listener in listeners:
            listener.close()


async def create_ephemeral_ca(directory: Path, name: str, hostname: str) -> tuple[Path, Path]:
    certificate = directory / f"{name}.pem"
    private_key = directory / f"{name}-key.pem"
    await asyncio.to_thread(
        subprocess.run,
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyCertSign,cRLSign",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        env=CLEAN_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    certificate.chmod(0o600)
    private_key.chmod(0o600)
    assert certificate.stat().st_mode & 0o777 == 0o600
    assert private_key.stat().st_mode & 0o777 == 0o600
    return certificate, private_key


def redacted_output(output: bytes, sensitive: list[bytes]) -> str:
    safe = output
    for value in sensitive:
        if value:
            safe = safe.replace(value, b"[redacted]")
            safe = safe.replace(base64.b64encode(value), b"[redacted]")
    return safe.decode(errors="replace")


def assert_no_sensitive_output(output: bytes, sensitive: list[bytes]) -> None:
    # A rewritten pytest `assert value not in output` can itself disclose the
    # matched value. Keep failed diagnostics independent of all private inputs.
    for value in sensitive:
        if value and (value in output or base64.b64encode(value) in output):
            raise AssertionError("private fixture data escaped the diagnostic boundary")


def assert_transition(
    events: list[AgentEvent], source: str, before: str, after: str, message: str | None = None
) -> AgentEvent:
    matches = [
        event
        for event in events
        if event.event_type == "work.transitioned"
        and event.source == source
        and event.payload.get("from") == before
        and event.payload.get("to") == after
    ]
    assert len(matches) == 1
    if message is not None:
        assert matches[0].message == message
    return matches[0]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_actual_guest_control_bootstrap_preserves_tls_and_release_gates(
    client, worker_headers, tmp_path, request, scenario
):
    config_path = Path(LAB_CONFIG)
    assert config_path.is_absolute()
    lab = json.loads(await asyncio.to_thread(config_path.read_text))
    assert lab["acknowledgement"] == "disposable-host-and-isolated-api-only"
    assert Path(lab["ssh_config"]).is_absolute()
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", lab["ssh_host"])
    assert re.fullmatch(r"/home/kelpie/worker-runner-api-[0-9]+\.test", lab["test_binary"])
    assert Path(lab["base_image"]).is_absolute()
    assert isinstance(lab["forward_port"], int) and 1024 <= lab["forward_port"] <= 65535
    assert lab["guest_ipv4"] == "192.168.5.2"
    tmp_path.chmod(0o700)
    assert tmp_path.stat().st_mode & 0o777 == 0o700

    ssh = [
        "ssh",
        "-F",
        lab["ssh_config"],
        "-S",
        "none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPersist=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "BatchMode=yes",
    ]
    resolved = await asyncio.create_subprocess_exec(
        *ssh,
        "-G",
        lab["ssh_host"],
        env=CLEAN_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        configuration, _ = await asyncio.wait_for(resolved.communicate(), 5)
    finally:
        await stop_owned_process(resolved)
    assert resolved.returncode == 0
    options = dict(line.split(" ", 1) for line in configuration.decode().splitlines())
    assert options["hostname"] == "127.0.0.1" and options["user"] == "kelpie"
    assert options["forwardagent"] == "no" and options["batchmode"] == "yes"

    created_files = [
        tmp_path / "control.pem",
        tmp_path / "control-key.pem",
        tmp_path / "unrelated.pem",
        tmp_path / "unrelated-key.pem",
    ]

    def remove_private_fixture_files() -> None:
        for path in created_files:
            path.unlink(missing_ok=True)

    request.addfinalizer(remove_private_fixture_files)
    server_cert, server_key = await create_ephemeral_ca(tmp_path, "control", "control.kelpie.test")
    unrelated_cert, unrelated_key = await create_ephemeral_ca(
        tmp_path, "unrelated", "unrelated.kelpie.test"
    )
    (
        server_ca,
        unrelated_ca,
        server_private_key,
        unrelated_private_key,
        runner_data,
    ) = await asyncio.gather(
        asyncio.to_thread(server_cert.read_bytes),
        asyncio.to_thread(unrelated_cert.read_bytes),
        asyncio.to_thread(server_key.read_bytes),
        asyncio.to_thread(unrelated_key.read_bytes),
        asyncio.to_thread(RUNNER_SOURCE.read_bytes),
    )
    assert server_ca != unrelated_ca
    guest_ca = unrelated_ca if scenario == "untrusted-ca" else server_ca

    work = await create_work(client, f"Actual guest control bootstrap: {scenario}")
    observation = TLSObservation(main.app, work["id"], scenario, client)
    listeners: list[socket.socket] = []
    servers: list[uvicorn.Server] = []
    tasks: list[asyncio.Task] = []
    forwarder = runner = health = None
    credential = worker_headers["Authorization"].removeprefix("Bearer ")
    output = b""
    try:
        worker_listener = create_listener()
        guest_listener = create_listener()
        listeners.extend([worker_listener, guest_listener])
        worker_server, worker_task = await start_server(main.app, worker_listener)
        servers.append(worker_server)
        tasks.append(worker_task)
        guest_server, guest_task = await start_server(
            observation,
            guest_listener,
            certificate=server_cert,
            private_key=server_key,
        )
        servers.append(guest_server)
        tasks.append(guest_task)
        worker_port = worker_listener.getsockname()[1]
        guest_port = guest_listener.getsockname()[1]

        forwarder = await asyncio.create_subprocess_exec(
            *ssh,
            "-o",
            "ExitOnForwardFailure=yes",
            "-N",
            "-R",
            f"127.0.0.1:{lab['forward_port']}:127.0.0.1:{worker_port}",
            lab["ssh_host"],
            env=CLEAN_ENV,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        health_command = shlex.join(
            [
                "env",
                "-i",
                "HOME=/home/kelpie",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "curl",
                "--noproxy",
                "*",
                "--retry",
                "5",
                "--retry-connrefused",
                "--retry-max-time",
                "8",
                "--max-time",
                "1",
                "--silent",
                "--fail",
                f"http://127.0.0.1:{lab['forward_port']}/healthz",
            ]
        )
        health = await asyncio.create_subprocess_exec(
            *ssh,
            lab["ssh_host"],
            health_command,
            env=CLEAN_ENV,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert await asyncio.wait_for(health.wait(), 12) == 0
        assert forwarder.returncode is None

        runner_command = shlex.join(
            [
                "sudo",
                "-n",
                "-u",
                "kelpie",
                "env",
                "-i",
                "HOME=/home/kelpie",
                "USER=kelpie",
                "LOGNAME=kelpie",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "KELPIE_LIBVIRT_TEST_ACK=disposable-host-only",
                "KELPIE_LIBVIRT_API_TEST_ACK=isolated-api-only",
                lab["test_binary"],
                "-test.run",
                "^TestDedicatedLibvirtRunnerAPI$",
                "-test.v",
            ]
        )
        runner = await asyncio.create_subprocess_exec(
            *ssh,
            lab["ssh_host"],
            runner_command,
            env=CLEAN_ENV,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        guest_hostname = (
            "wrong-control.kelpie.test" if scenario == "wrong-hostname" else "control.kelpie.test"
        )
        payload = json.dumps(
            {
                "control_url": f"http://127.0.0.1:{lab['forward_port']}",
                "guest_url": f"https://{guest_hostname}:{guest_port}",
                "guest_ipv4": lab["guest_ipv4"],
                "credential": credential,
                "work_id": work["id"],
                "base_image": lab["base_image"],
                "ca": base64.b64encode(guest_ca).decode(),
                "runner": base64.b64encode(runner_data).decode(),
                "scenario": scenario,
            }
        ).encode()
        sensitive = [
            credential.encode(),
            server_ca,
            unrelated_ca,
            guest_ca,
            server_private_key,
            unrelated_private_key,
        ]
        try:
            output, _ = await asyncio.wait_for(runner.communicate(payload), 390)
        except TimeoutError as error:
            await stop_owned_process(runner)
            output, _ = await asyncio.wait_for(runner.communicate(), 3)
            print(redacted_output(output, sensitive))
            assert_no_sensitive_output(output, sensitive)
            raise AssertionError("actual Runner/Executor drill exceeded its outer bound") from error
        print(redacted_output(output, sensitive))
        assert_no_sensitive_output(output, sensitive)
        assert runner.returncode == 0, "actual Runner/Executor drill failed; journal retained"

        async for session in main.app.dependency_overrides[get_session]():
            item = await session.get(WorkItem, work["id"])
            events = list(
                await session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.work_item_id == work["id"])
                    .order_by(AgentEvent.id)
                )
            )
            lease = await session.scalar(
                select(ResourceLease).where(ResourceLease.work_item_id == work["id"])
            )

        assert isinstance(item, WorkItem) and isinstance(lease, ResourceLease)
        assert all(isinstance(event, AgentEvent) for event in events)
        event_types = [event.event_type for event in events]
        print(f"Real API event types ({scenario}):", event_types)
        assert event_types.count("lease.released") == 1
        assert "repository.cloned" not in event_types
        assert lease.state == "released"

        if scenario in {"trusted", "cancelled"}:
            assert observation.requests == observation.run_requests
            assert observation.requests == observation.lease_requests
            assert observation.requests >= 2
            assert ("GET", f"/api/runs/{work['id']}", 200) in observation.responses
            assert all(
                path.startswith(f"/api/runs/{work['id']}") for _, path, _ in observation.responses
            )
            assert_transition(
                events,
                source=f"worker:{lease.worker_id}",
                before="provisioning",
                after="analyzing",
                message="Runner control bootstrap completed",
            )
            assert not any(event.event_type == "worker.failed" for event in events)
        else:
            assert observation.requests == observation.run_requests == 0
            assert observation.lease_requests == 0
            assert observation.responses == []
            assert not any(
                event.event_type == "work.transitioned" and event.payload.get("to") == "analyzing"
                for event in events
            )
            failures = [event for event in events if event.event_type == "worker.failed"]
            assert len(failures) == 1
            assert failures[0].source == "worker"
            assert failures[0].message == "VM Runner control bootstrap was not confirmed"
            assert not any(event.event_type == "runner.failed" for event in events)

        if scenario == "trusted":
            failures = [event for event in events if event.event_type == "runner.failed"]
            assert len(failures) == 1 and failures[0].source == "vm-runner"
            assert failures[0].message in {
                "git clone failed with exit code 128",
                "git clone timed out after 120 seconds",
            }
            assert item.status == WorkStatus.FAILED and item.version == 4
            assert_transition(
                events,
                source=f"worker:{lease.worker_id}",
                before="analyzing",
                after="failed",
            )
        elif scenario == "cancelled":
            assert observation.cancellations == 1
            assert item.status == WorkStatus.CANCELLED and item.version == 4
            assert_transition(
                events,
                source=f"worker:{lease.worker_id}",
                before="analyzing",
                after="cancelled",
                message="Authenticated execution cancellation after bootstrap",
            )
            assert not any(
                event.event_type == "work.transitioned" and event.payload.get("to") == "failed"
                for event in events
            )
        else:
            assert item.status == WorkStatus.FAILED and item.version == 3
            assert_transition(
                events,
                source=f"worker:{lease.worker_id}",
                before="provisioning",
                after="failed",
                message="Worker executor failed",
            )
    finally:
        await stop_owned_process(runner)
        await stop_owned_process(health)
        await stop_owned_process(forwarder)
        await stop_servers(servers, tasks, listeners)
