import asyncio
import os
import re
import signal
import tempfile
from contextlib import suppress
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .integrations.github import GitHubAppClient
from .models import DeliveryBundle, DeliveryJob, WorkItem, WorkStatus, utcnow
from .observability import observe_delivery_attempt, observe_delivery_outcome, tracer
from .schemas import EventCreate
from .service import emit_event, transition_work_item

settings = get_settings()
github = GitHubAppClient(settings)


async def run_command(
    *command: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        # Cancellation must stop git and its children before a delivery guard unlocks.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        raise
    text = output.decode(errors="replace")
    if process.returncode != 0:
        safe_command = " ".join(command[:2])
        raise RuntimeError(f"{safe_command} failed with {process.returncode}: {text[-4000:]}")
    return text


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
    async with SessionLocal() as session:
        work = await session.get(WorkItem, work_item_id)
    attributes = {"kelpie.work_id": work_item_id}
    if work is not None:
        attributes["kelpie.correlation_id"] = work.correlation_id
    with tracer.start_as_current_span("delivery.run", attributes=attributes):
        await _deliver_work(work_item_id)


async def _deliver_work(work_item_id: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(DeliveryJob, work_item_id, with_for_update=True)
        work = await session.get(WorkItem, work_item_id)
        bundle = await session.get(DeliveryBundle, work_item_id)
        if not job or not work or not bundle or job.state not in {"pending", "retry"}:
            return
        if work.status != WorkStatus.COMMITTING:
            return
        attempt_type = "retry" if job.attempts > 0 or job.state == "retry" else "initial"
        job.state = "running"
        job.attempts += 1
        job.updated_at = utcnow()
        await session.commit()
        observe_delivery_attempt(attempt_type)

    try:
        if not work.github_installation_id:
            raise RuntimeError("repository has no GitHub App installation")
        token = await github.installation_token(work.github_installation_id)
        metadata = await github.repository(work.repository, token)
        base_branch = metadata["default_branch"]
        target_branch = branch_name(work)
        owner = work.repository.split("/", 1)[0]
        pull_request_url = await github.find_pull_request(
            work.repository, token, owner=owner, head=target_branch
        )
        branch_exists = pull_request_url is None and await github.branch_exists(
            work.repository, token, target_branch
        )
        if pull_request_url is None and not branch_exists:
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
                await run_command("git", "checkout", "-b", target_branch, cwd=repository)
                await run_command(
                    "git", "apply", "--index", "--binary", bundle.object_path, cwd=repository
                )
                await run_command(
                    "git", "config", "user.name", settings.git_bot_name, cwd=repository
                )
                await run_command(
                    "git", "config", "user.email", settings.git_bot_email, cwd=repository
                )
                await run_command("git", "commit", "-m", work.title, cwd=repository)
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
            pull_request_url = await github.create_pull_request(
                work.repository,
                token,
                title=work.title,
                head=target_branch,
                base=base_branch,
                body=pull_request_body(work),
            )
        async with SessionLocal() as session:
            current = await session.get(WorkItem, work_item_id, with_for_update=True)
            job = await session.get(DeliveryJob, work_item_id, with_for_update=True)
            if not current or not job:
                return
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
    except Exception as error:
        span = trace.get_current_span()
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
        async with SessionLocal() as session:
            current = await session.get(WorkItem, work_item_id, with_for_update=True)
            job = await session.get(DeliveryJob, work_item_id, with_for_update=True)
            if job:
                job.state = "failed"
                job.error = str(error)[:4000]
                job.updated_at = utcnow()
            if current:
                await emit_event(
                    session,
                    work_item_id,
                    EventCreate(
                        event_type="delivery.failed",
                        source="delivery:github",
                        level="error",
                        message=str(error)[:4000],
                    ),
                )
                if current.status == WorkStatus.COMMITTING:
                    await transition_work_item(
                        session,
                        current,
                        WorkStatus.FAILED,
                        expected_version=current.version,
                        actor="delivery:github",
                        message="GitHub delivery failed",
                    )
            await session.commit()
        observe_delivery_outcome("failed")


async def resume_pending_deliveries() -> None:
    async with SessionLocal() as session:
        jobs = list(
            (
                await session.scalars(
                    select(DeliveryJob).where(
                        DeliveryJob.state.in_({"pending", "retry", "running"})
                    )
                )
            ).all()
        )
        for job in jobs:
            if job.state == "running":
                job.state = "retry"
                job.error = "control plane restarted during delivery"
        await session.commit()
    for job in jobs:
        asyncio.create_task(deliver_work(job.work_item_id))


def pull_request_body(work: WorkItem) -> str:
    issue = f"\n\nCloses #{work.github_issue_number}" if work.github_issue_number else ""
    return (
        "## Summary\n\n"
        f"Automated implementation for: {work.requirement[:4000]}\n\n"
        "## Verification\n\n"
        "See the linked Kelpie run for tests and review artifacts."
        f"{issue}"
    )
