import asyncio
import base64
import json
import mimetypes
import os
import shlex
import signal
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass
class Assignment:
    id: str
    correlation_id: str
    title: str
    requirement: str
    repository: str
    version: int
    budget_minutes: int
    replan_limit: int

    @classmethod
    def from_environment(cls) -> "Assignment":
        encoded = os.environ["KELPIE_ASSIGNMENT"]
        data = json.loads(base64.urlsafe_b64decode(encoded).decode())
        return cls(**{field: data[field] for field in cls.__dataclass_fields__})


class ControlClient:
    def __init__(self, base_url: str, work_id: str, lease: str, correlation_id: str):
        self.work_id = work_id
        self._lease = lease
        ca_file = os.environ.get("KELPIE_CONTROL_CA_FILE")
        # Explicit per-API trust only. Never disable certificate/name checks or
        # alter the guest system/model provider's trust configuration.
        verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "X-Kelpie-Lease": lease,
                "X-Kelpie-Correlation-ID": correlation_id,
            },
            timeout=30,
            verify=verify,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def redact(self, value: Any) -> Any:
        return redact(value, lease=self._lease)

    async def event(
        self,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        source: str = "vm-runner",
        payload: dict[str, Any] | None = None,
    ) -> None:
        response = await self.client.post(
            f"/api/runs/{self.work_id}/events",
            json=self.redact({
                "event_type": event_type,
                "source": source,
                "level": level,
                "message": message,
                "payload": payload or {},
            }),
        )
        response.raise_for_status()

    async def transition(self, status: str, version: int, message: str) -> dict:
        response = await self.client.post(
            f"/api/runs/{self.work_id}/transition",
            json={
                "status": status,
                "expected_version": version,
                "message": self.redact(message),
                "payload": {},
            },
        )
        response.raise_for_status()
        return response.json()

    async def commands(self, after_feedback: int, after_approval: int) -> dict:
        response = await self.client.get(
            f"/api/runs/{self.work_id}/commands",
            params={"after_feedback": after_feedback, "after_approval": after_approval},
        )
        response.raise_for_status()
        return response.json()

    async def fail_execution(self) -> None:
        # A stale assignment must not overwrite cancellation, approval waiting,
        # or central delivery. Re-read once on an optimistic-lock conflict;
        # preserve the API's authority instead of forcing a terminal state.
        for attempt in range(2):
            response = await self.client.get(f"/api/runs/{self.work_id}")
            response.raise_for_status()
            work = response.json()
            if (
                not isinstance(work, dict)
                or work.get("id") != self.work_id
                or type(work.get("status")) is not str
                or type(work.get("version")) is not int
                or work["version"] < 1
            ):
                return
            if work["status"] not in {"provisioning", "analyzing", "implementing", "verifying"}:
                return
            try:
                await self.transition("failed", work["version"], "Runner execution failed")
                return
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 409 or attempt == 1:
                    raise

    async def upload_delivery_bundle(self, content: bytes) -> dict:
        response = await self.client.post(
            f"/api/runs/{self.work_id}/delivery-bundle",
            content=content,
            headers={"Content-Type": "text/x-diff"},
        )
        response.raise_for_status()
        return response.json()

    async def upload_artifact(self, path: Path) -> dict:
        content = await asyncio.to_thread(path.read_bytes)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = await self.client.post(
            f"/api/runs/{self.work_id}/artifacts/upload",
            params={"name": path.name, "kind": "verification", "content_type": content_type},
            content=content,
            headers={"Content-Type": content_type},
        )
        response.raise_for_status()
        return response.json()


class CodexAppServer:
    def __init__(self, cwd: Path, control: ControlClient):
        self.cwd = cwd
        self.control = control
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.next_id = 10

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "kelpie",
                        "title": "Kelpie Autonomous Development Runner",
                        "version": "0.1.0",
                    }
                },
            }
        )
        await self._send({"method": "initialized", "params": {}})
        await self._send({"method": "thread/start", "id": 2, "params": {"cwd": str(self.cwd)}})
        while self.thread_id is None:
            message = await self._read()
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError(
                        f"Codex thread/start failed: {self.control.redact(message['error'])}"
                    )
                self.thread_id = message["result"]["thread"]["id"]

    async def run_turn(self, prompt: str) -> None:
        if self.thread_id is None:
            raise RuntimeError("Codex session not initialized")
        request_id = self.next_id
        self.next_id += 1
        await self._send(
            {
                "method": "turn/start",
                "id": request_id,
                "params": {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
            }
        )
        while True:
            message = await self._read()
            method = message.get("method", "")
            if method.endswith("requestApproval") and "id" in message:
                await self._send({"id": message["id"], "result": {"decision": "acceptForSession"}})
            if method:
                await self.control.event(
                    "codex." + method.replace("/", "."),
                    event_message(self.control.redact(message)),
                    source="codex",
                    payload=message.get("params", {}),
                )
            if method == "turn/completed":
                status = message.get("params", {}).get("turn", {}).get("status")
                if status not in {None, "completed"}:
                    raise RuntimeError(f"Codex turn finished with status {status}")
                return

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()

    async def _send(self, message: dict) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex process is not running")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def _read(self) -> dict:
        if not self.process or not self.process.stdout:
            raise RuntimeError("Codex process is not running")
        line = await self.process.stdout.readline()
        if not line:
            stderr = ""
            if self.process.stderr:
                stderr = self.control.redact(
                    (await self.process.stderr.read()).decode(errors="replace")
                )[-4000:]
            raise RuntimeError(f"Codex App Server exited unexpectedly: {stderr}")
        return json.loads(line)


def event_message(message: dict) -> str:
    params = message.get("params", {})
    item = params.get("item", {}) if isinstance(params, dict) else {}
    for value in (
        item.get("text"),
        item.get("command"),
        params.get("message") if isinstance(params, dict) else None,
        message.get("method"),
    ):
        if value:
            return str(value)[:4000]
    return "Codex event"


def redact(value: Any, *, lease: str = "") -> Any:
    """Redact credential fields and the known lease, not arbitrary secret formats.

    Only telemetry copies are changed. The lease header and process inputs retain
    their original values; encoded/unknown credentials require separate scanning.
    """
    sensitive = {
        "token", "accesstoken", "refreshtoken", "idtoken", "sessiontoken",
        "authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
        "secret", "clientsecret", "password", "passwd", "privatekey",
        "leasetoken", "xkelpielease", "kelpieleasetoken",
    }
    if isinstance(value, dict):
        return {
            redact(key, lease=lease): (
                "[REDACTED]" if key.lower().replace("_", "").replace("-", "") in sensitive
                else redact(item, lease=lease)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, lease=lease) for item in value]
    if isinstance(value, str) and lease:
        return value.replace(lease, "[REDACTED]")
    return value


def verification_commands(repository: Path) -> list[list[str]]:
    config_path = repository / ".kelpie.yaml"
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text()) or {}
        commands = config.get("verification", {}).get("commands", [])
        if commands:
            return [
                shlex.split(command) if isinstance(command, str) else command
                for command in commands
            ]
    commands: list[list[str]] = []
    if (repository / "pyproject.toml").exists():
        commands.append([sys.executable, "-m", "pytest"])
    if (repository / "go.mod").exists():
        commands.append(["go", "test", "./..."])
    package = repository / "package.json"
    if package.exists():
        scripts = json.loads(package.read_text()).get("scripts", {})
        for name in ("lint", "typecheck", "test", "build"):
            if name in scripts:
                commands.append(["npm", "run", name])
    return commands


async def run_verification(repository: Path, control: ControlClient) -> tuple[bool, str]:
    failures: list[str] = []
    commands = verification_commands(repository)
    if not commands:
        return False, "No verification commands were detected; add .kelpie.yaml"
    for command in commands:
        await control.event(
            "verification.started", shlex.join(command), payload={"command": command}
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=repository,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=900)
        except TimeoutError:
            process.kill()
            await process.wait()
            failures.append(f"{shlex.join(command)} timed out")
            continue
        # Sanitize before retaining the tail so slicing cannot split the lease.
        text = control.redact(output.decode(errors="replace"))[-20_000:]
        await control.event(
            "verification.completed",
            f"{shlex.join(command)} exited {process.returncode}",
            level="info" if process.returncode == 0 else "error",
            payload={"command": command, "exit_code": process.returncode, "output": text},
        )
        if process.returncode != 0:
            failures.append(f"$ {shlex.join(command)}\n{text}")
    return not failures, "\n\n".join(failures)


async def create_delivery_bundle(repository: Path) -> bytes:
    intent = await asyncio.create_subprocess_exec(
        "git",
        "add",
        "--intent-to-add",
        "--",
        ".",
        cwd=repository,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    intent_output, _ = await intent.communicate()
    if intent.returncode != 0:
        raise RuntimeError(
            "failed to prepare delivery bundle: "
            + intent_output.decode(errors="replace")[-4000:]
        )
    diff = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--binary",
        "--full-index",
        "HEAD",
        "--",
        ".",
        ":(exclude).kelpie/**",
        cwd=repository,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    content, _ = await diff.communicate()
    if diff.returncode != 0:
        raise RuntimeError(
            "failed to create delivery bundle: " + content.decode(errors="replace")[-4000:]
        )
    if not content:
        raise RuntimeError("implementation produced no deliverable repository changes")
    return content


def evidence_files(repository: Path) -> list[Path]:
    root = repository / ".kelpie" / "artifacts"
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".txt", ".json"}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in allowed_suffixes:
            continue
        resolved = path.resolve()
        if resolved.is_relative_to(resolved_root) and path.stat().st_size <= 10 * 1024 * 1024:
            files.append(path)
    return files


async def upload_evidence(repository: Path, control: ControlClient) -> None:
    for path in await asyncio.to_thread(evidence_files, repository):
        await control.upload_artifact(path)


def initial_prompt(assignment: Assignment) -> str:
    return f"""You are the main development agent for Kelpie work item {assignment.id}.

Repository: {assignment.repository}
Title: {assignment.title}
Requirement:
{assignment.requirement}

Analyze the repository, create a concrete plan, implement the complete
requirement, and run relevant checks. You may create subagents for independent
work. Use browser/computer tools when needed and save evidence under
.kelpie/artifacts. Do not commit, push, open a pull request, or expose
credentials. The control plane performs that only after human approval. Do not
stop at an explanation: modify the working tree and leave it ready for
independent verification.
"""


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Lack of signal permission is not proof of absence.
    return True


def wait_for_process_group_exit(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0, timeout)
    while process_group_exists(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))
    return True


async def join_process_group(
    process: asyncio.subprocess.Process, process_group: int, join_seconds: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0, join_seconds)
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0, join_seconds))
    except TimeoutError:
        return False
    remaining = max(0, deadline - asyncio.get_running_loop().time())
    return await asyncio.to_thread(wait_for_process_group_exit, process_group, remaining)


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    terminate_timeout: float = 5,
    kill_timeout: float = 5,
) -> None:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        await join_process_group(process, process_group, terminate_timeout)
        return
    if await join_process_group(process, process_group, terminate_timeout):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await join_process_group(process, process_group, kill_timeout)


async def clone_repository(
    assignment: Assignment, root: Path, timeout_seconds: float = 120,
) -> Path:
    target = root / "repository"
    clone_url = os.getenv("KELPIE_CLONE_URL", f"https://github.com/{assignment.repository}.git")
    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--",
        clone_url,
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError as error:
        raise RuntimeError(
            f"git clone timed out after {timeout_seconds:g} seconds"
        ) from error
    finally:
        # A helper can outlive a parent which exited normally or failed. Every
        # exit path must settle this clone's private process group.
        await terminate_process_group(process)
    if process.returncode != 0:
        # Clone output can echo credential-bearing URLs. Keep the exit code for
        # diagnosis without retaining or emitting subprocess output.
        raise RuntimeError(f"git clone failed with exit code {process.returncode}")
    return target


async def run() -> None:
    assignment = Assignment.from_environment()
    control = ControlClient(
        os.environ["KELPIE_CONTROL_URL"],
        assignment.id,
        os.environ["KELPIE_LEASE_TOKEN"],
        assignment.correlation_id,
    )
    session: CodexAppServer | None = None
    try:
        work_root = Path(os.getenv("KELPIE_WORK_ROOT", "/workspace")) / assignment.id
        work_root.mkdir(parents=True, exist_ok=True)
        repository = await clone_repository(assignment, work_root)
        await control.event("repository.cloned", assignment.repository)
        work = await control.transition("implementing", assignment.version, "Repository ready")
        session = CodexAppServer(repository, control)
        await session.start()
        prompt = initial_prompt(assignment)
        attempts = 0
        while True:
            await session.run_turn(prompt)
            work = await control.transition(
                "verifying", work["version"], "Running independent checks"
            )
            passed, output = await run_verification(repository, control)
            if passed:
                await upload_evidence(repository, control)
                await control.upload_delivery_bundle(
                    await create_delivery_bundle(repository)
                )
                work = await control.transition(
                    "awaiting_approval", work["version"], "All detected checks passed"
                )
                break
            attempts += 1
            if attempts > assignment.replan_limit:
                await control.transition(
                    "budget_exhausted",
                    work["version"],
                    "Automatic repair attempts exhausted",
                )
                break
            work = await control.transition(
                "implementing", work["version"], f"Verification failed; repair attempt {attempts}"
            )
            prompt = (
                "Independent verification failed. Fix every failure and rerun "
                "focused checks:\n\n" + output
            )

        feedback_cursor = 0
        approval_cursor = 0
        while True:
            commands = await control.commands(feedback_cursor, approval_cursor)
            if commands["approvals"]:
                approval_cursor = commands["approvals"][-1]["id"]
            if commands["status"] == "committing":
                await control.event(
                    "delivery.ready",
                    "Human approval recorded; the delivery adapter may commit and open the PR",
                    level="warning",
                )
                return
            if commands["status"] in {"cancelled", "failed", "pr_created", "completed"}:
                return
            # Feedback and approval rows are history, not permission to execute.
            # Keep pending feedback unread until the control plane authorizes resumption.
            if commands["status"] == "implementing":
                prompt = (
                    "Continue implementation authorized by the control plane. Reassess the work."
                )
                if commands["feedback"]:
                    feedback_cursor = commands["feedback"][-1]["id"]
                    prompt = "Apply this user feedback completely:\n\n" + "\n".join(
                        entry["message"] for entry in commands["feedback"]
                    )
                if not passed:
                    prompt += "\n\nFix the latest independent verification failures:\n\n" + output
                work = {"version": commands["version"]}
                await session.run_turn(prompt)
                work = await control.transition(
                    "verifying", work["version"], "Verifying authorized changes"
                )
                passed, output = await run_verification(repository, control)
                repair_attempts = 0
                while not passed:
                    repair_attempts += 1
                    if repair_attempts > assignment.replan_limit:
                        work = await control.transition(
                            "budget_exhausted",
                            work["version"],
                            "Authorized repair attempts exhausted",
                        )
                        break
                    work = await control.transition(
                        "implementing",
                        work["version"],
                        f"Verification failed; authorized repair attempt {repair_attempts}",
                    )
                    await session.run_turn("Fix these verification failures:\n\n" + output)
                    work = await control.transition(
                        "verifying", work["version"], "Rechecking authorized repair"
                    )
                    passed, output = await run_verification(repository, control)
                if passed:
                    await upload_evidence(repository, control)
                    await control.upload_delivery_bundle(
                        await create_delivery_bundle(repository)
                    )
                    work = await control.transition(
                        "awaiting_approval",
                        work["version"],
                        "Authorized revision verification completed",
                    )
                continue
            await asyncio.sleep(3)
    except Exception as error:
        try:
            await control.event("runner.failed", str(error), level="error")
        except Exception:
            # Event transport failure must not prevent the independent state
            # update or replace the original execution error.
            pass
        try:
            await control.fail_execution()
        except Exception:
            # No unbounded retries or release from inside the VM. The Worker
            # remains responsible for physical cleanup and lease accounting.
            pass
        raise
    finally:
        if session:
            await session.close()
        await control.close()


def entrypoint() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    entrypoint()
