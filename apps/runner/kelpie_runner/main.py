import asyncio
import base64
import json
import mimetypes
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass
class Assignment:
    id: str
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
    def __init__(self, base_url: str, work_id: str, lease: str):
        self.work_id = work_id
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Kelpie-Lease": lease},
            timeout=30,
        )

    async def close(self) -> None:
        await self.client.aclose()

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
            json={
                "event_type": event_type,
                "source": source,
                "level": level,
                "message": message,
                "payload": payload or {},
            },
        )
        response.raise_for_status()

    async def transition(self, status: str, version: int, message: str) -> dict:
        response = await self.client.post(
            f"/api/runs/{self.work_id}/transition",
            json={
                "status": status,
                "expected_version": version,
                "message": message,
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
                    raise RuntimeError(f"Codex thread/start failed: {message['error']}")
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
                    event_message(message),
                    source="codex",
                    payload=redact(message.get("params", {})),
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
                stderr = (await self.process.stderr.read()).decode(errors="replace")[-4000:]
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


def redact(value: Any) -> Any:
    sensitive = {"token", "authorization", "apikey", "api_key", "secret", "password"}
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in sensitive else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
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
        text = output.decode(errors="replace")[-20_000:]
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


async def clone_repository(assignment: Assignment, root: Path) -> Path:
    target = root / "repository"
    clone_url = os.getenv("KELPIE_CLONE_URL", f"https://github.com/{assignment.repository}.git")
    process = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--",
        clone_url,
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"git clone failed: {output.decode(errors='replace')[-4000:]}")
    return target


async def run() -> None:
    assignment = Assignment.from_environment()
    control = ControlClient(
        os.environ["KELPIE_CONTROL_URL"], assignment.id, os.environ["KELPIE_LEASE_TOKEN"]
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
            if commands["feedback"]:
                feedback_cursor = commands["feedback"][-1]["id"]
                prompt = "Apply this user feedback completely:\n\n" + "\n".join(
                    entry["message"] for entry in commands["feedback"]
                )
                work = {"version": commands["version"]}
                await session.run_turn(prompt)
                work = await control.transition(
                    "verifying", work["version"], "Verifying feedback changes"
                )
                passed, output = await run_verification(repository, control)
                repair_attempts = 0
                while not passed:
                    repair_attempts += 1
                    if repair_attempts > assignment.replan_limit:
                        work = await control.transition(
                            "budget_exhausted",
                            work["version"],
                            "Feedback repair attempts exhausted",
                        )
                        break
                    work = await control.transition(
                        "implementing",
                        work["version"],
                        f"Feedback verification failed; repair attempt {repair_attempts}",
                    )
                    await session.run_turn("Fix these verification failures:\n\n" + output)
                    work = await control.transition(
                        "verifying", work["version"], "Rechecking feedback repair"
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
                        "Feedback verification completed",
                    )
                continue
            if commands["approvals"]:
                approval_cursor = commands["approvals"][-1]["id"]
            if commands["status"] == "committing":
                await control.event(
                    "delivery.ready",
                    "Human approval recorded; the delivery adapter may commit and open the PR",
                    level="warning",
                )
                return
            if commands["status"] in {"cancelled", "failed", "completed"}:
                return
            await asyncio.sleep(3)
    except Exception as error:
        try:
            await control.event("runner.failed", str(error), level="error")
        finally:
            raise
    finally:
        if session:
            await session.close()
        await control.close()


def entrypoint() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    entrypoint()
