import asyncio
import os
import re
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import SessionLocal
from .integrations.github import GitHubAppClient
from .models import DeliveryBundle, DeliveryJob, WorkerHost, WorkItem, WorkStatus, utcnow
from .observability import observe_delivery_attempt, observe_delivery_outcome, tracer
from .schemas import EventCreate
from .service import emit_event, transition_work_item

settings = get_settings()
github = GitHubAppClient(settings)
DELIVERY_WRITE_SECONDS = 45
DELIVERY_RECOVERY_DB_SECONDS = 2
_active_deliveries: set[str] = set()


class DeliveryStopped(Exception):
    """The work no longer permits publication; do not overwrite its current outcome."""


class DeliveryCommandError(RuntimeError):
    """A subprocess failed; its arguments and output must remain private."""


def delivery_error_code(error: Exception) -> str:
    if isinstance(error, DeliveryCommandError):
        return "command_failed"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPError):
        return "upstream_error"
    if isinstance(error, OSError):
        return "filesystem_error"
    return "internal_error"


async def lock_delivery(
    session: AsyncSession, work_item_id: str, *, states: tuple[str, ...] = ("running",),
) -> tuple[WorkItem, DeliveryJob]:
    worker_id = await session.scalar(select(WorkItem.assigned_worker_id)
                                    .where(WorkItem.id == work_item_id))
    if worker_id is None:
        raise DeliveryStopped
    # All publication and quarantine paths use Worker -> Work -> DeliveryJob ordering.
    worker = await session.get(WorkerHost, worker_id, with_for_update=True, populate_existing=True)
    if worker is None or worker.quarantined_at is not None:
        raise DeliveryStopped
    work = await session.get(WorkItem, work_item_id, with_for_update=True, populate_existing=True)
    job = await session.get(DeliveryJob, work_item_id, with_for_update=True, populate_existing=True)
    if (work is None or job is None or work.assigned_worker_id != worker_id
            or work.status != WorkStatus.COMMITTING or job.state not in states):
        raise DeliveryStopped
    return work, job


@asynccontextmanager
async def guard_delivery_write(work_item_id: str) -> AsyncIterator[None]:
    # Quarantine waits for an already-started write. After it commits, no new write starts.
    async with asyncio.timeout(DELIVERY_WRITE_SECONDS):
        async with SessionLocal() as session:
            await lock_delivery(session, work_item_id)
            yield


async def run_command(
    *command: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        raise DeliveryCommandError("delivery subprocess could not start") from None
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        # Cancellation must stop git and its children before a delivery guard unlocks.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        raise
    if process.returncode != 0:
        raise DeliveryCommandError(
            f"delivery subprocess failed with exit code {process.returncode}",
        )
    return output.decode(errors="replace")


def branch_name(work: WorkItem) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", work.title.lower()).strip("-")[:40]
    return f"agent/{work.id[:8]}-{slug or 'change'}"


def prepare_delivery_workspace(
    root: str,
) -> tuple[tempfile.TemporaryDirectory, Path, Path]:
    artifact_root = Path(root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="delivery-", dir=artifact_root)
    temp = Path(temporary.name)
    repository = temp / "repository"
    askpass = temp / "askpass.sh"
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" "x-access-token" ;;\n'
        '*) printf "%s\\n" "$KELPIE_GIT_PASSWORD" ;;\nesac\n'
    )
    askpass.chmod(0o700)
    return temporary, repository, askpass


async def deliver_work(work_item_id: str) -> None:
    await _exclusive_delivery(work_item_id, recover_running=False)


async def _exclusive_delivery(work_item_id: str, *, recover_running: bool) -> None:
    # Reserve before the first await: request delivery and startup recovery share this guard.
    # This is process-local, not a distributed lease; the MVP runs one API process.
    if work_item_id in _active_deliveries:
        return
    _active_deliveries.add(work_item_id)
    try:
        if recover_running:
            try:
                async with asyncio.timeout(DELIVERY_RECOVERY_DB_SECONDS):
                    async with SessionLocal() as session:
                        _, job = await lock_delivery(
                            session, work_item_id, states=("pending", "retry", "running"),
                        )
                        if job.state == "running":
                            job.state = "retry"
                            job.error = "control plane restarted during delivery"
                        await session.commit()
            except DeliveryStopped:
                return
        await _traced_delivery(work_item_id)
    finally:
        _active_deliveries.remove(work_item_id)


async def _traced_delivery(work_item_id: str) -> None:
    async with SessionLocal() as session:
        work = await session.get(WorkItem, work_item_id)
    attributes = {"kelpie.work_id": work_item_id}
    if work is not None:
        attributes["kelpie.correlation_id"] = work.correlation_id
    with tracer.start_as_current_span("delivery.run", attributes=attributes):
        await _deliver_work(work_item_id)


async def _deliver_work(work_item_id: str) -> None:
    try:
        async with SessionLocal() as session:
            work, job = await lock_delivery(session, work_item_id, states=("pending", "retry"))
            bundle = await session.get(DeliveryBundle, work_item_id)
            if bundle is None:
                return
            attempt_type = "retry" if job.attempts > 0 or job.state == "retry" else "initial"
            job.state = "running"
            job.attempts += 1
            job.updated_at = utcnow()
            await session.commit()
            observe_delivery_attempt(attempt_type)
    except DeliveryStopped:
        return

    stage = "configuration"
    try:
        if not work.github_installation_id:
            raise RuntimeError("repository has no GitHub App installation")
        stage = "token"
        async with guard_delivery_write(work_item_id):
            token = await github.installation_token(work.github_installation_id)
        stage = "metadata"
        metadata = await github.repository(work.repository, token)
        base_branch = metadata["default_branch"]
        target_branch = branch_name(work)
        owner = work.repository.split("/", 1)[0]
        stage = "existing_pull_request"
        pull_request_url = await github.find_pull_request(
            work.repository, token, owner=owner, head=target_branch
        )
        stage = "existing_branch"
        branch_exists = pull_request_url is None and await github.branch_exists(
            work.repository, token, target_branch
        )
        if pull_request_url is None and not branch_exists:
            stage = "workspace"
            temporary, repository, askpass = await asyncio.to_thread(
                prepare_delivery_workspace, settings.artifact_root
            )
            try:
                environment = {
                    **os.environ,
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                    "KELPIE_GIT_PASSWORD": token,
                }
                stage = "clone"
                await run_command(
                    "git",
                    "clone",
                    "--branch",
                    base_branch,
                    "--single-branch",
                    "--",
                    f"https://github.com/{work.repository}.git",
                    str(repository),
                    environment=environment,
                )
                stage = "checkout"
                await run_command("git", "checkout", "-b", target_branch, cwd=repository)
                stage = "apply"
                await run_command(
                    "git", "apply", "--index", "--binary", bundle.object_path, cwd=repository
                )
                stage = "commit"
                await run_command(
                    "git", "config", "user.name", settings.git_bot_name, cwd=repository
                )
                await run_command(
                    "git", "config", "user.email", settings.git_bot_email, cwd=repository
                )
                await run_command("git", "commit", "-m", work.title, cwd=repository)
                stage = "push"
                async with guard_delivery_write(work_item_id):
                    await run_command(
                        "git",
                        "push",
                        "--set-upstream",
                        "origin",
                        target_branch,
                        cwd=repository,
                        environment=environment,
                    )
            finally:
                await asyncio.to_thread(temporary.cleanup)
        if pull_request_url is None:
            stage = "pull_request"
            async with guard_delivery_write(work_item_id):
                pull_request_url = await github.create_pull_request(
                    work.repository,
                    token,
                    title=work.title,
                    head=target_branch,
                    base=base_branch,
                    body=pull_request_body(work),
                )
        stage = "finalize"
        async with SessionLocal() as session:
            current, job = await lock_delivery(session, work_item_id)
            current.pull_request_url = pull_request_url
            await transition_work_item(
                session,
                current,
                WorkStatus.PR_CREATED,
                expected_version=current.version,
                actor="delivery:github",
                message=f"Pull request created: {pull_request_url}",
            )
            await transition_work_item(
                session,
                current,
                WorkStatus.COMPLETED,
                expected_version=current.version,
                actor="delivery:github",
                message="Delivery completed",
            )
            job.state = "completed"
            job.error = None
            await session.commit()
            observe_delivery_outcome("completed")
    except DeliveryStopped:
        return
    except Exception as error:
        error_code = delivery_error_code(error)
        message = f"GitHub delivery failed at {stage} ({error_code})"
        span = trace.get_current_span()
        span.set_attribute("kelpie.delivery.stage", stage)
        span.set_attribute("kelpie.delivery.error_code", error_code)
        # Do not export upstream messages, arguments, paths, or chained tracebacks.
        span.record_exception(RuntimeError(message))
        span.set_status(Status(StatusCode.ERROR, message))
        try:
            async with SessionLocal() as session:
                current, job = await lock_delivery(session, work_item_id)
                job.state = "failed"
                job.error = message
                job.updated_at = utcnow()
                await emit_event(
                    session,
                    work_item_id,
                    EventCreate(
                        event_type="delivery.failed",
                        source="delivery:github",
                        level="error",
                        message=message,
                        payload={"stage": stage, "error_code": error_code},
                    ),
                )
                await transition_work_item(
                    session,
                    current,
                    WorkStatus.FAILED,
                    expected_version=current.version,
                    actor="delivery:github",
                    message="GitHub delivery failed",
                )
                await session.commit()
        except DeliveryStopped:
            return
        observe_delivery_outcome("failed")


async def resume_pending_deliveries() -> None:
    async with asyncio.timeout(DELIVERY_RECOVERY_DB_SECONDS):
        async with SessionLocal() as session:
            identifiers = list((await session.scalars(select(DeliveryJob.work_item_id).where(
                DeliveryJob.state.in_({"pending", "retry", "running"}),
            ))).all())
    # Keep ownership until all children finish; cancellation joins them before shutdown.
    results = await asyncio.gather(*(
        _exclusive_delivery(work_item_id, recover_running=True) for work_item_id in identifiers
    ), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        # The startup supervisor retries. Never log raw DB/connection exception details.
        raise RuntimeError("delivery recovery did not finish")


def pull_request_body(work: WorkItem) -> str:
    issue = f"\n\nCloses #{work.github_issue_number}" if work.github_issue_number else ""
    return (
        "## Summary\n\n"
        f"Automated implementation for: {work.requirement[:4000]}\n\n"
        "## Verification\n\n"
        "See the linked Kelpie run for tests and review artifacts."
        f"{issue}"
    )
