import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from anyio import CancelScope
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .artifact_cache import ArtifactCacheMiddleware
from .artifact_content import ALLOWED_ARTIFACT_TYPES, artifact_content_matches
from .artifact_names import artifact_disposition, valid_artifact_name
from .artifact_storage import (
    MAX_ARTIFACT_BYTES,
    ArtifactStorageError,
    artifact_path,
    read_artifact_content,
    write_artifact_content,
)
from .audit import (
    ApprovalState,
    ConsoleOwnership,
    record_approval_audit,
    record_cancellation_audit,
    record_console_audit,
    record_feedback_audit,
)
from .auth import (
    Actor,
    actor_from_identity,
    bound_worker,
    current_actor,
    require_gateway,
    require_worker,
)
from .authorization import (
    authorize_repository,
    authorized_work,
    authorized_work_with_decision,
    development_repository,
    slack_actor,
)
from .bundle_storage import MAX_BUNDLE_BYTES, BundleIntegrityError, verified_bundle_bytes
from .config import Settings, get_settings
from .correlation import CorrelationMiddleware
from .db import (
    SchemaReadiness,
    SessionLocal,
    bootstrap_schema,
    get_schema_readiness,
    get_session,
)
from .delivery import deliver_work, resume_pending_deliveries
from .integrations.github import GitHubAppClient
from .integrations.slack import SlackNotifier, verify_signature
from .models import (
    AgentEvent,
    Approval,
    Artifact,
    AuditRecord,
    AuthSession,
    ConsoleLease,
    DeliveryBundle,
    DeliveryJob,
    Feedback,
    OIDCLoginAttempt,
    PreviewEndpoint,
    Repository,
    Role,
    WebhookDelivery,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkSource,
    WorkStatus,
    utcnow,
)
from .observability import (
    DELIVERY_RECOVERY,
    RUNTIME_HEALTH,
    ObservabilityMiddleware,
    configure_observability,
    metrics_payload,
    observe_approval,
)
from .oidc import (
    OIDCAuthenticationError,
    OIDCConfigurationError,
    OIDCProvider,
    code_challenge,
    get_oidc_provider,
)
from .runtime_monitor import monitor_runtime_health
from .schemas import (
    ApprovalCreate,
    ArtifactCreate,
    ArtifactView,
    AuditRecordView,
    ClaimRequest,
    ClaimResponse,
    ConsoleLeaseRequest,
    ConsoleLeaseView,
    EventCreate,
    EventView,
    FeedbackCreate,
    PreviewCreate,
    PreviewView,
    TransitionRequest,
    WorkCancellationRequest,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerView,
    WorkItemCreate,
    WorkItemView,
)
from .secrets import SecretUnavailableError
from .service import (
    cancel_queued_work,
    claim_next_work,
    create_work_item,
    emit_event,
    ensure_feedback_allowed,
    transition_work_item,
    validate_lease,
)
from .worker_quarantine import ensure_worker_not_quarantined

logger = logging.getLogger(__name__)
DELIVERY_RECOVERY_RETRY_SECONDS = 5
STREAM_READ_SECONDS = 2


async def recover_startup_deliveries() -> None:
    try:
        while True:
            try:
                if (await get_schema_readiness()).ready:
                    DELIVERY_RECOVERY.running()
                    await resume_pending_deliveries()
                    DELIVERY_RECOVERY.complete()
                    return
                DELIVERY_RECOVERY.database_unready()
            except Exception:
                DELIVERY_RECOVERY.error()
                # Exception strings can include DB URLs or query parameters.
                logger.warning("delivery recovery failed; retrying")
            await asyncio.sleep(DELIVERY_RECOVERY_RETRY_SECONDS)
    except asyncio.CancelledError:
        DELIVERY_RECOVERY.cancel()
        raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime_settings = get_settings()
    configure_observability(runtime_settings)
    if runtime_settings.database_schema_mode == "bootstrap":
        await bootstrap_schema()
    readiness = await get_schema_readiness()
    if not readiness.ready:
        logger.warning(
            "database schema is not ready: state=%s current_heads=%s expected_heads=%s",
            readiness.state,
            readiness.current_heads,
            readiness.expected_heads,
        )
    DELIVERY_RECOVERY.start()
    RUNTIME_HEALTH.reset()
    recovery = asyncio.create_task(recover_startup_deliveries(), name="delivery-startup-recovery")
    monitor = asyncio.create_task(
        monitor_runtime_health(RUNTIME_HEALTH, runtime_settings), name="runtime-health-monitor",
    )
    try:
        yield
    finally:
        recovery.cancel()
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
        RUNTIME_HEALTH.unavailable()  # Also covers cancellation before the task's first step.
        with suppress(asyncio.CancelledError):
            await recovery
        if recovery.cancelled():
            # A task cancelled before its first step cannot execute its own cleanup.
            DELIVERY_RECOVERY.cancel()


app = FastAPI(title="Kelpie Control Plane", version="0.1.0", lifespan=lifespan)
settings = get_settings()
slack = SlackNotifier(settings)
github = GitHubAppClient(settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Kelpie-Correlation-ID"],
)
app.add_middleware(ArtifactCacheMiddleware)
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(CorrelationMiddleware)


@app.exception_handler(SecretUnavailableError)
async def secret_unavailable(_: Request, __: SecretUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "configured secret is unavailable"},
        headers={"Cache-Control": "no-store"},
    )

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Actor, Depends(current_actor)]
SchemaReadinessDep = Annotated[SchemaReadiness, Depends(get_schema_readiness)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
OIDCProviderDep = Annotated[OIDCProvider, Depends(get_oidc_provider)]


async def get_work_item(
    session: AsyncSession, work_item_id: str, *, lock: bool = False
) -> WorkItem:
    statement = select(WorkItem).where(WorkItem.id == work_item_id)
    if lock:
        statement = statement.with_for_update()
    item = (await session.execute(statement)).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
    return item


async def validate_delivery_ready(session: AsyncSession, item: WorkItem, config: Settings) -> bool:
    """Return whether central GitHub delivery is required for this run."""
    worker = (
        await session.get(WorkerHost, item.assigned_worker_id)
        if item.assigned_worker_id
        else None
    )
    if worker is not None and worker.labels.get("virtualization") == "mock":
        return False
    bundle = await session.get(DeliveryBundle, item.id)
    if bundle is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the runner has not uploaded a verified delivery bundle",
        )
    if not item.github_installation_id or not github.configured:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "GitHub App installation is not configured for this repository",
        )
    try:
        await asyncio.to_thread(verified_bundle_bytes, config.artifact_root,
                                bundle.object_path, bundle.sha256, bundle.size_bytes)
    except BundleIntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "delivery bundle is unavailable or invalid") from None
    return True


async def queue_delivery(
    session: AsyncSession, item: WorkItem, *, approval_audit_id: int,
) -> None:
    job = await session.get(DeliveryJob, item.id)
    if job is None:
        session.add(DeliveryJob(work_item_id=item.id, state="pending",
                                approval_audit_id=approval_audit_id))
        return
    if job.state != "completed":
        job.state = "retry"
        job.error = None
        job.approval_audit_id = approval_audit_id


def write_delivery_bundle(root: str, work_item_id: str, content: bytes) -> Path:
    directory = Path(root) / work_item_id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "delivery.patch"
    temporary = directory / f"delivery.patch.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    temporary.write_bytes(content)
    os.replace(temporary, destination)
    return destination


def ensure_allowed_preview_target(target_url: str, allowed_cidrs: list[str]) -> None:
    hostname = urlsplit(target_url).hostname
    try:
        address = ipaddress.ip_address(hostname or "")
        networks = [ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs]
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "preview target must use a literal IP in an allowed VM network",
        ) from error
    if not any(address in network for network in networks):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "preview target is outside the allowed VM networks",
        )


def validate_artifact_content(content_type: str, content: bytes) -> None:
    if not artifact_content_matches(content_type, content):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "artifact content does not match its declared type",
        )


def require_oidc_mode(config: Settings) -> None:
    if config.auth_mode != "oidc":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC authentication is not enabled")


def safe_return_path(value: str) -> str:
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    content, content_type = metrics_payload()
    return Response(content=content, headers={"Content-Type": content_type})


@app.get("/readyz")
async def readyz(response: Response, readiness: SchemaReadinessDep) -> dict[str, str]:
    if not readiness.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database_schema": readiness.state}
    return {"status": "ok", "database_schema": readiness.state}


@app.get("/auth/login", include_in_schema=False)
async def oidc_login(
    session: SessionDep,
    config: SettingsDep,
    provider: OIDCProviderDep,
    return_to: str = "/",
) -> RedirectResponse:
    require_oidc_mode(config)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    try:
        authorization_url = await provider.authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=code_challenge(verifier),
        )
    except OIDCConfigurationError as error:
        logger.warning("OIDC login configuration failed: %s", error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "identity provider is unavailable",
        ) from error
    session.add(
        OIDCLoginAttempt(
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            nonce=nonce,
            code_verifier=verifier,
            return_to=safe_return_path(return_to),
            expires_at=datetime.now(UTC) + timedelta(seconds=config.oidc_login_ttl_seconds),
        )
    )
    await session.commit()
    response = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        config.oidc_login_cookie_name,
        state,
        max_age=config.oidc_login_ttl_seconds,
        httponly=True,
        secure=config.oidc_cookie_secure,
        samesite="lax",
        path="/auth/callback",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/callback", include_in_schema=False)
async def oidc_callback(
    request: Request,
    session: SessionDep,
    config: SettingsDep,
    provider: OIDCProviderDep,
    code: str = "",
    state_value: Annotated[str, Query(alias="state")] = "",
    error: str = "",
) -> RedirectResponse:
    require_oidc_mode(config)
    cookie_state = request.cookies.get(config.oidc_login_cookie_name, "")
    if error or not code or not state_value or not cookie_state:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC authentication failed")
    if not hmac.compare_digest(state_value, cookie_state):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC state does not match")

    state_hash = hashlib.sha256(state_value.encode()).hexdigest()
    attempt = await session.get(OIDCLoginAttempt, state_hash, with_for_update=True)
    if attempt is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC login attempt is invalid")
    await session.delete(attempt)
    await session.commit()
    if aware_utc(attempt.expires_at) <= datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC login attempt expired")

    try:
        identity = await provider.authenticate(
            code=code,
            code_verifier=attempt.code_verifier,
            expected_nonce=attempt.nonce,
        )
    except OIDCConfigurationError as provider_error:
        logger.warning("OIDC callback configuration failed: %s", provider_error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "identity provider is unavailable",
        ) from provider_error
    except OIDCAuthenticationError as authentication_error:
        logger.info("OIDC callback rejected: %s", authentication_error)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "OIDC authentication failed",
        ) from authentication_error

    now = datetime.now(UTC)
    expires_at = min(
        identity.expires_at,
        now + timedelta(seconds=config.oidc_session_ttl_seconds),
    )
    if expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC ID token expired")

    await actor_from_identity(session, identity.issuer, identity.subject, identity.organization)
    previous_token = request.cookies.get(config.oidc_session_cookie_name)
    if previous_token:
        previous_hash = hashlib.sha256(previous_token.encode()).hexdigest()
        previous_session = await session.get(AuthSession, previous_hash)
        if previous_session is not None:
            await session.delete(previous_session)
    session_token = secrets.token_urlsafe(32)
    session.add(
        AuthSession(
            token_hash=hashlib.sha256(session_token.encode()).hexdigest(),
            subject=identity.subject,
            identity_provider=identity.issuer,
            organization=identity.organization,
            expires_at=expires_at,
        )
    )
    await session.commit()

    redirect_target = f"{config.dashboard_url.rstrip('/')}{attempt.return_to}"
    response = RedirectResponse(redirect_target, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(config.oidc_login_cookie_name, path="/auth/callback")
    response.set_cookie(
        config.oidc_session_cookie_name,
        session_token,
        max_age=max(0, int((expires_at - now).total_seconds())),
        httponly=True,
        secure=config.oidc_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/session", include_in_schema=False)
async def auth_session(actor: ActorDep) -> dict[str, str]:
    return {
        "subject": actor.subject,
        "identity_provider": actor.identity_provider,
        "organization": actor.organization,
        "role": actor.role,
    }


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def logout(request: Request, session: SessionDep, config: SettingsDep) -> Response:
    token = request.cookies.get(config.oidc_session_cookie_name)
    if token:
        authenticated = await session.get(AuthSession, hashlib.sha256(token.encode()).hexdigest())
        if authenticated is not None:
            await session.delete(authenticated)
            await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(config.oidc_session_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/work-items", response_model=WorkItemView, status_code=status.HTTP_201_CREATED)
async def submit_work(
    payload: WorkItemCreate, session: SessionDep, actor: ActorDep, config: SettingsDep,
) -> WorkItem:
    if config.auth_mode == "development":
        await development_repository(session, config.development_organization, payload.repository)
    repository = await authorize_repository(session, actor, payload.repository, Role.OPERATOR)
    installation_id = repository.github_installation_id
    if config.auth_mode == "development":
        installation_id = await github.installation_for_repository(payload.repository)
    item = await create_work_item(
        session,
        payload,
        source=WorkSource.WEB,
        requested_by=actor.subject,
        organization_id=actor.organization,
        github_installation_id=installation_id,
    )
    await session.commit()
    return item


@app.get("/api/work-items", response_model=list[WorkItemView])
async def list_work_items(
    session: SessionDep,
    actor: ActorDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkItem]:
    statement = (
        select(WorkItem).join(Repository, Repository.name == WorkItem.repository)
        .where(WorkItem.organization_id == actor.organization,
               Repository.organization_id == actor.organization)
        .order_by(WorkItem.created_at.desc()).limit(limit)
    )
    return list((await session.scalars(statement)).all())


@app.get("/api/work-items/{work_item_id}", response_model=WorkItemView)
async def read_work_item(work_item_id: str, session: SessionDep, actor: ActorDep) -> WorkItem:
    return await authorized_work(session, actor, work_item_id)


@app.get("/api/work-items/{work_item_id}/event-log", response_model=list[EventView])
async def event_log(
    work_item_id: str,
    session: SessionDep,
    actor: ActorDep,
    after: Annotated[int, Query(ge=0)] = 0,
) -> list[AgentEvent]:
    await authorized_work(session, actor, work_item_id)
    statement = (
        select(AgentEvent)
        .where(AgentEvent.work_item_id == work_item_id, AgentEvent.id > after)
        .order_by(AgentEvent.id)
        .limit(1000)
    )
    return list((await session.scalars(statement)).all())


@app.get("/api/work-items/{work_item_id}/audit-log", response_model=list[AuditRecordView])
async def audit_log(
    work_item_id: str, session: SessionDep, actor: ActorDep, response: Response,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditRecord]:
    await authorized_work(session, actor, work_item_id, Role.ADMINISTRATOR)
    response.headers["Cache-Control"] = "no-store"
    statement = select(AuditRecord).where(
        AuditRecord.organization_id == actor.organization,
        AuditRecord.work_item_id == work_item_id, AuditRecord.id > after,
    ).order_by(AuditRecord.id).limit(limit)
    return list((await session.scalars(statement)).all())


@app.get("/api/work-items/{work_item_id}/events")
async def stream_events(
    request: Request,
    work_item_id: str,
    session: SessionDep,
    actor: ActorDep,
    config: SettingsDep,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    await authorized_work(session, actor, work_item_id)
    cursor = max(after, int(last_event_id or 0))

    async def generate():
        nonlocal cursor
        idle_ticks = 0
        while True:
            try:
                # AnyIO disconnects cancel repeatedly at every await; let a bounded read
                # and its connection cleanup finish before honoring that cancellation.
                with CancelScope(shield=True):
                    async with asyncio.timeout(STREAM_READ_SECONDS):
                        async with SessionLocal() as event_session:
                            stream_actor = await current_actor(request, config, event_session)
                            await authorized_work(event_session, stream_actor, work_item_id)
                            statement = (
                                select(AgentEvent)
                                .where(AgentEvent.work_item_id == work_item_id,
                                       AgentEvent.id > cursor)
                                .order_by(AgentEvent.id)
                                .limit(100)
                            )
                            events = list((await event_session.scalars(statement)).all())
            except HTTPException:
                return
            except (TimeoutError, SQLAlchemyError):
                # Do not expose SQL, bound parameters or connection details in stream errors.
                logger.warning("event stream read failed; closing stream")
                return
            # Never yield while a shield is active, or publish a batch after disconnect.
            await asyncio.sleep(0)
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = event.id
                    data = EventView.model_validate(event).model_dump_json()
                    yield f"id: {event.id}\ndata: {data}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    yield ": keepalive\n\n"
                    idle_ticks = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@app.post("/api/work-items/{work_item_id}/cancel", response_model=WorkItemView)
async def cancel_work(
    request: Request,
    work_item_id: str,
    payload: WorkCancellationRequest,
    session: SessionDep,
    actor: ActorDep,
) -> WorkItem:
    item, decision = await authorized_work_with_decision(
        session, actor, work_item_id, Role.ADMINISTRATOR, lock=True,
    )
    version_before = item.version
    await cancel_queued_work(session, item, expected_version=payload.expected_version,
                             actor=actor.subject)
    record_cancellation_audit(session, request, item, actor, decision,
                              version_before=version_before)
    await session.commit()
    return item


@app.post("/api/work-items/{work_item_id}/feedback", response_model=WorkItemView)
async def add_feedback(
    request: Request,
    work_item_id: str,
    payload: FeedbackCreate,
    session: SessionDep,
    actor: ActorDep,
) -> WorkItem:
    item, decision = await authorized_work_with_decision(
        session, actor, work_item_id, Role.OPERATOR, lock=True,
    )
    await ensure_worker_not_quarantined(session, item)
    ensure_feedback_allowed(item)
    feedback = Feedback(
        work_item_id=work_item_id, actor=actor.subject,
        channel=payload.channel, message=payload.message,
    )
    session.add(feedback)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="feedback.received",
            source=actor.subject,
            message=payload.message,
            payload={"channel": payload.channel},
        ),
    )
    if item.status in {
        WorkStatus.AWAITING_FEEDBACK,
        WorkStatus.AWAITING_APPROVAL,
        WorkStatus.AWAITING_INPUT,
    }:
        await transition_work_item(
            session,
            item,
            WorkStatus.IMPLEMENTING,
            expected_version=item.version,
            actor=actor.subject,
            message="Feedback received; resuming implementation",
        )
    record_feedback_audit(session, request, item, actor, decision, feedback, transport="web")
    await session.commit()
    return item


@app.post("/api/work-items/{work_item_id}/approvals", response_model=WorkItemView)
async def decide_approval(
    work_item_id: str,
    payload: ApprovalCreate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    background_tasks: BackgroundTasks,
    config: SettingsDep,
) -> WorkItem:
    item, decision = await authorized_work_with_decision(
        session, actor, work_item_id, Role.APPROVER, lock=True,
    )
    await ensure_worker_not_quarantined(session, item)
    before = ApprovalState.capture(item)
    target: WorkStatus | None = None
    should_deliver = False
    if payload.kind == "pull_request":
        if item.status != WorkStatus.AWAITING_APPROVAL:
            raise HTTPException(status.HTTP_409_CONFLICT, "work is not awaiting PR approval")
        if payload.decision == "approve":
            should_deliver = await validate_delivery_ready(session, item, config)
            target = WorkStatus.COMMITTING
        else:
            target = WorkStatus.IMPLEMENTING
    elif payload.kind == "budget":
        if item.status != WorkStatus.BUDGET_EXHAUSTED:
            raise HTTPException(status.HTTP_409_CONFLICT, "work has not exhausted its budget")
        if payload.decision == "approve":
            extension = payload.payload.get("minutes", 60)
            if type(extension) is not int or not 15 <= extension <= 24 * 60:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid budget extension"
                )
            item.budget_minutes += extension
            target = WorkStatus.IMPLEMENTING
    approval = Approval(
        work_item_id=work_item_id,
        kind=payload.kind,
        decision=payload.decision,
        actor=actor.subject,
        payload=payload.payload,
    )
    session.add(approval)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="approval.decided",
            source=actor.subject,
            message=f"{payload.kind}: {payload.decision}",
            payload=payload.model_dump(),
        ),
    )
    if target is not None:
        await transition_work_item(
            session,
            item,
            target,
            expected_version=item.version,
            actor=actor.subject,
        )
    approval_audit = await record_approval_audit(
        session, request, item, actor, decision, approval, before,
        transport="web", delivery_queued=should_deliver,
    )
    if should_deliver:
        await queue_delivery(session, item, approval_audit_id=approval_audit.id)
    await session.commit()
    observe_approval(payload.kind, payload.decision)
    if should_deliver:
        background_tasks.add_task(deliver_work, item.id)
    if item.status in {WorkStatus.COMMITTING, WorkStatus.IMPLEMENTING}:
        background_tasks.add_task(slack.post_status, item)
    return item


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: SessionDep,
    config: SettingsDep,
    github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    delivery_id: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> Response:
    body = await request.body()
    expected = (
        "sha256="
        + hmac.new(config.read_secret("github_webhook_secret", required=True).encode(),
                   body, hashlib.sha256).hexdigest()
    )
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature")
    if not github_event or not delivery_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing GitHub headers")
    if await session.get(WebhookDelivery, delivery_id):
        return Response(status_code=status.HTTP_200_OK)
    session.add(WebhookDelivery(delivery_id=delivery_id, event_name=github_event))
    payload = json.loads(body)
    issue = payload.get("issue", {})
    labels = {label.get("name") for label in issue.get("labels", [])}
    eligible = github_event == "issues" and payload.get("action") in {"opened", "labeled"}
    if not eligible or config.agent_trigger_label not in labels:
        await session.commit()
        return Response(status_code=status.HTTP_202_ACCEPTED)
    repository = payload.get("repository", {}).get("full_name", "").lower()
    number = issue.get("number")
    if not repository or not number:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid issue payload")
    registered = await session.get(Repository, repository)
    installation_id = payload.get("installation", {}).get("id")
    if (registered is None or registered.github_installation_id is None
            or type(installation_id) is not int
            or installation_id != registered.github_installation_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "repository installation is not registered")
    source_id = f"github:{repository}:{number}"
    existing = (
        await session.execute(select(WorkItem).where(WorkItem.source_external_id == source_id))
    ).scalar_one_or_none()
    if existing is None:
        work_payload = WorkItemCreate(
            title=issue.get("title") or f"Issue #{number}",
            requirement=issue.get("body") or issue.get("title") or "Implement the issue",
            repository=repository,
        )
        await create_work_item(
            session,
            work_payload,
            source=WorkSource.GITHUB,
            requested_by=issue.get("user", {}).get("login", "github"),
            organization_id=registered.organization_id,
            source_external_id=source_id,
            github_installation_id=payload.get("installation", {}).get("id"),
            github_issue_number=number,
        )
    await session.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@app.post("/api/workers/register", response_model=WorkerView)
async def register_worker(
    payload: WorkerRegistration,
    session: SessionDep,
    identity: Annotated[WorkerHost | None, Depends(require_worker)],
) -> WorkerHost:
    if identity is not None and identity.name != payload.name:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "worker credential scope mismatch")
    worker = (
        await session.execute(select(WorkerHost).where(WorkerHost.name == payload.name)
                              .with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if worker is None:
        worker = WorkerHost(
            **payload.model_dump(),
            cpu_available=payload.cpu_total,
            memory_mb_available=payload.memory_mb_total,
        )
        session.add(worker)
    else:
        worker = await bound_worker(session, worker.id, identity)
        if worker.cpu_total == 0 and worker.memory_mb_total == 0 and worker.active_runs == 0:
            worker.cpu_available = payload.cpu_total
            worker.memory_mb_available = payload.memory_mb_total
        worker.cpu_total = payload.cpu_total
        worker.memory_mb_total = payload.memory_mb_total
        if worker.active_runs == 0:
            worker.disk_gb_available = payload.disk_gb_available
        worker.labels = payload.labels
        worker.last_seen_at = utcnow()
        if worker.state == WorkerState.OFFLINE:
            worker.state = WorkerState.ONLINE
    await session.commit()
    return worker


@app.post("/api/workers/{worker_id}/heartbeat", response_model=WorkerView)
async def worker_heartbeat(
    worker_id: str,
    payload: WorkerHeartbeat,
    session: SessionDep,
    identity: Annotated[WorkerHost | None, Depends(require_worker)],
) -> WorkerHost:
    worker = await bound_worker(session, worker_id, identity)
    for name, value in payload.model_dump().items():
        setattr(worker, name, value)
    worker.last_seen_at = utcnow()
    await session.commit()
    return worker


@app.post("/api/workers/{worker_id}/claim", response_model=ClaimResponse | None)
async def claim_work(
    worker_id: str,
    payload: ClaimRequest,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    identity: Annotated[WorkerHost | None, Depends(require_worker)],
) -> ClaimResponse | None:
    worker = await bound_worker(session, worker_id, identity)
    result = await claim_next_work(session, worker, payload, config.lease_seconds)
    await session.commit()
    if result is None:
        return None
    item, token, lease = result
    return ClaimResponse(work_item=item, lease_token=token, lease_expires_at=lease.expires_at)


@app.post("/api/runs/{work_item_id}/events", response_model=EventView)
async def ingest_worker_event(
    work_item_id: str,
    payload: EventCreate,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> AgentEvent:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    await get_work_item(session, work_item_id)
    event = await emit_event(session, work_item_id, payload)
    await session.commit()
    return event


@app.post("/api/runs/{work_item_id}/transition", response_model=WorkItemView)
async def worker_transition(
    work_item_id: str,
    payload: TransitionRequest,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    background_tasks: BackgroundTasks,
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> WorkItem:
    lease = await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    item = await get_work_item(session, work_item_id, lock=True)
    await transition_work_item(
        session,
        item,
        payload.status,
        expected_version=payload.expected_version,
        actor=f"worker:{lease.worker_id}",
        message=payload.message,
        payload=payload.payload,
    )
    await session.commit()
    if item.status in {
        WorkStatus.AWAITING_APPROVAL,
        WorkStatus.BUDGET_EXHAUSTED,
        WorkStatus.FAILED,
        WorkStatus.COMPLETED,
    }:
        background_tasks.add_task(slack.post_status, item)
    return item


@app.post("/webhooks/slack/commands")
async def slack_command(
    request: Request,
    session: SessionDep,
    background_tasks: BackgroundTasks,
    config: SettingsDep,
    timestamp: Annotated[str | None, Header(alias="X-Slack-Request-Timestamp")] = None,
    signature: Annotated[str | None, Header(alias="X-Slack-Signature")] = None,
) -> dict:
    body = await request.body()
    verify_signature(body, timestamp, signature, config.read_secret("slack_signing_secret"))
    form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
    user_id = form.get("user_id", "unknown")
    parts = form.get("text", "").strip().split(maxsplit=2)
    usage = "Usage: /kelpie feedback <work-id> <message> or /kelpie approve <work-id>"
    if len(parts) < 2:
        return {"response_type": "ephemeral", "text": usage}
    action, work_item_id = parts[:2]
    identity = await slack_actor(session, form.get("team_id", ""), user_id)
    required = Role.APPROVER if action == "approve" else Role.OPERATOR
    item, decision = await authorized_work_with_decision(
        session, identity, work_item_id, required, lock=True,
    )
    await ensure_worker_not_quarantined(session, item)
    actor = identity.principal_id
    if action == "feedback" and len(parts) == 3:
        ensure_feedback_allowed(item)
        message = parts[2]
        feedback = Feedback(
            work_item_id=work_item_id, actor=actor, channel="slack", message=message,
        )
        session.add(feedback)
        await emit_event(
            session,
            work_item_id,
            EventCreate(
                event_type="feedback.received",
                source=actor,
                message=message,
                payload={"channel": "slack"},
            ),
        )
        if item.status in {
            WorkStatus.AWAITING_FEEDBACK,
            WorkStatus.AWAITING_APPROVAL,
            WorkStatus.AWAITING_INPUT,
        }:
            await transition_work_item(
                session,
                item,
                WorkStatus.IMPLEMENTING,
                expected_version=item.version,
                actor=actor,
                message="Slack feedback received; resuming implementation",
            )
        record_feedback_audit(
            session, request, item, identity, decision, feedback, transport="slack",
        )
        await session.commit()
        return {"response_type": "ephemeral", "text": f"Feedback sent to {item.title}."}
    if action == "approve":
        if item.status != WorkStatus.AWAITING_APPROVAL:
            raise HTTPException(status.HTTP_409_CONFLICT, "work is not awaiting approval")
        should_deliver = await validate_delivery_ready(session, item, config)
        before = ApprovalState.capture(item)
        approval = Approval(
            work_item_id=work_item_id,
            kind="pull_request",
            decision="approve",
            actor=actor,
            payload={},
        )
        session.add(approval)
        await transition_work_item(
            session,
            item,
            WorkStatus.COMMITTING,
            expected_version=item.version,
            actor=actor,
            message="Commit and pull request approved from Slack",
        )
        approval_audit = await record_approval_audit(
            session, request, item, identity, decision, approval, before,
            transport="slack", delivery_queued=should_deliver,
        )
        if should_deliver:
            await queue_delivery(session, item, approval_audit_id=approval_audit.id)
        await session.commit()
        observe_approval("pull_request", "approve")
        if should_deliver:
            background_tasks.add_task(deliver_work, item.id)
        return {"response_type": "ephemeral", "text": f"Approved {item.title}."}
    return {"response_type": "ephemeral", "text": usage}


@app.get("/api/runs/{work_item_id}", response_model=WorkItemView)
async def lease_read_work(
    work_item_id: str,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> WorkItem:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    item = await get_work_item(session, work_item_id)
    await session.commit()
    return item


@app.get("/api/runs/{work_item_id}/commands")
async def runner_commands(
    work_item_id: str,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    after_feedback: Annotated[int, Query(ge=0)] = 0,
    after_approval: Annotated[int, Query(ge=0)] = 0,
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> dict:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    item = await get_work_item(session, work_item_id)
    feedback_statement = (
        select(Feedback)
        .where(Feedback.work_item_id == work_item_id, Feedback.id > after_feedback)
        .order_by(Feedback.id)
    )
    approval_statement = (
        select(Approval)
        .where(Approval.work_item_id == work_item_id, Approval.id > after_approval)
        .order_by(Approval.id)
    )
    feedback = list((await session.scalars(feedback_statement)).all())
    approvals = list((await session.scalars(approval_statement)).all())
    await session.commit()
    return {
        "status": item.status.value,
        "version": item.version,
        "feedback": [
            {
                "id": entry.id,
                "actor": entry.actor,
                "channel": entry.channel,
                "message": entry.message,
            }
            for entry in feedback
        ],
        "approvals": [
            {
                "id": entry.id,
                "actor": entry.actor,
                "kind": entry.kind,
                "decision": entry.decision,
                "payload": entry.payload,
            }
            for entry in approvals
        ],
    }


@app.post("/api/runs/{work_item_id}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_run(
    work_item_id: str,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> Response:
    lease = await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    item = await get_work_item(session, work_item_id, lock=True)
    if item.status not in {WorkStatus.COMPLETED, WorkStatus.FAILED, WorkStatus.CANCELLED}:
        raise HTTPException(status.HTTP_409_CONFLICT, "only terminal work can release its lease")
    worker = await session.get(WorkerHost, lease.worker_id)
    if worker is not None:
        worker.cpu_available = min(worker.cpu_total, worker.cpu_available + lease.cpu)
        worker.memory_mb_available = min(
            worker.memory_mb_total, worker.memory_mb_available + lease.memory_mb
        )
        worker.disk_gb_available += lease.disk_gb
        worker.active_runs = max(0, worker.active_runs - 1)
    lease.state = "released"
    await emit_event(
        session,
        work_item_id,
        EventCreate(event_type="lease.released", message="Worker resources released"),
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/runs/{work_item_id}/delivery-bundle", status_code=status.HTTP_201_CREATED)
async def upload_delivery_bundle(
    work_item_id: str,
    request: Request,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> dict[str, str | int]:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    item = await get_work_item(session, work_item_id, lock=True)
    if item.status not in {WorkStatus.VERIFYING, WorkStatus.AWAITING_APPROVAL}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "delivery bundles are accepted only after implementation verification",
        )
    content_length = request.headers.get("content-length")
    maximum_size = MAX_BUNDLE_BYTES
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "invalid content-length"
            ) from error
        if declared_size > maximum_size:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "bundle exceeds 20 MiB")
    content = await request.body()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "delivery bundle is empty")
    if len(content) > maximum_size:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "bundle exceeds 20 MiB")
    checksum = hashlib.sha256(content).hexdigest()
    path = await asyncio.to_thread(
        write_delivery_bundle, config.artifact_root, work_item_id, content
    )
    bundle = await session.get(DeliveryBundle, work_item_id)
    if bundle is None:
        bundle = DeliveryBundle(
            work_item_id=work_item_id,
            object_path=str(path),
            sha256=checksum,
            size_bytes=len(content),
        )
        session.add(bundle)
    else:
        bundle.object_path = str(path)
        bundle.sha256 = checksum
        bundle.size_bytes = len(content)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="delivery.bundle_uploaded",
            source="vm-runner",
            message="Verified patch bundle uploaded",
            payload={"sha256": checksum, "size_bytes": len(content)},
        ),
    )
    await session.commit()
    return {"sha256": checksum, "size_bytes": len(content)}


@app.get("/api/work-items/{work_item_id}/delivery-bundle")
async def download_delivery_bundle(
    work_item_id: str,
    session: SessionDep,
    actor: ActorDep,
    config: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await authorized_work(session, actor, work_item_id)
    bundle = await session.get(DeliveryBundle, work_item_id)
    if bundle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery bundle not found")
    try:
        content = await asyncio.to_thread(verified_bundle_bytes, config.artifact_root,
                                         bundle.object_path, bundle.sha256, bundle.size_bytes)
    except BundleIntegrityError:
        raise HTTPException(status.HTTP_410_GONE, "delivery bundle is unavailable") from None
    return Response(
        content=content,
        media_type="text/x-diff",
        headers={"Content-Disposition": f'attachment; filename="{work_item_id}.patch"'},
    )


@app.post("/api/runs/{work_item_id}/artifacts", status_code=status.HTTP_201_CREATED)
async def register_artifact(
    work_item_id: str,
    payload: ArtifactCreate,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> dict[str, str]:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    await get_work_item(session, work_item_id)
    try:
        artifact_path(work_item_id, payload.object_key)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "artifact key must belong to this work") from None
    if not valid_artifact_name(payload.name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid artifact name")
    if payload.content_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported artifact type")
    artifact = Artifact(work_item_id=work_item_id, **payload.model_dump())
    session.add(artifact)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="artifact.created",
            message=payload.name,
            payload={"artifact_id": artifact.id, "kind": payload.kind},
        ),
    )
    await session.commit()
    return {"id": artifact.id}


@app.post("/api/runs/{work_item_id}/artifacts/upload", status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    work_item_id: str,
    request: Request,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    background_tasks: BackgroundTasks,
    name: Annotated[str, Query(min_length=1, max_length=255)],
    kind: Annotated[str, Query(min_length=1, max_length=64)] = "evidence",
    content_type: Annotated[str, Query(min_length=1, max_length=128)] = "image/png",
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> ArtifactView:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    await get_work_item(session, work_item_id)
    if not valid_artifact_name(name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid artifact name")
    if content_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported artifact type")
    maximum_size = MAX_ARTIFACT_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdecimal() and int(declared) > maximum_size:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact exceeds 10 MiB")
    content = await request.body()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "artifact is empty")
    if len(content) > maximum_size:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact exceeds 10 MiB")
    validate_artifact_content(content_type, content)
    artifact_id = str(uuid.uuid4())
    suffix = Path(name).suffix.lower()[:12]
    object_key = f"{work_item_id}/artifacts/{artifact_id}{suffix}"
    try:
        path = await asyncio.to_thread(
            write_artifact_content, config.artifact_root, work_item_id, object_key, content,
        )
    except ArtifactStorageError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "artifact storage is unavailable") from None
    artifact = Artifact(
        id=artifact_id,
        work_item_id=work_item_id,
        kind=kind,
        name=name,
        content_type=content_type,
        object_key=object_key,
        size_bytes=len(content),
    )
    session.add(artifact)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="artifact.uploaded",
            source="vm-runner",
            message=name,
            payload={"artifact_id": artifact_id, "kind": kind, "content_type": content_type},
        ),
    )
    await session.commit()
    if content_type.startswith("image/"):
        background_tasks.add_task(slack.upload_image, path, f"{name} · {work_item_id[:8]}")
    return ArtifactView.model_validate(artifact)


@app.get("/api/work-items/{work_item_id}/artifacts", response_model=list[ArtifactView])
async def list_artifacts(
    work_item_id: str,
    session: SessionDep,
    actor: ActorDep,
) -> list[Artifact]:
    await authorized_work(session, actor, work_item_id)
    statement = (
        select(Artifact)
        .where(Artifact.work_item_id == work_item_id)
        .order_by(Artifact.created_at.desc())
    )
    return list((await session.scalars(statement)).all())


@app.get("/api/work-items/{work_item_id}/artifacts/{artifact_id}")
async def download_artifact(
    work_item_id: str,
    artifact_id: str,
    session: SessionDep,
    actor: ActorDep,
    config: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await authorized_work(session, actor, work_item_id)
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None or artifact.work_item_id != work_item_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    if artifact.content_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(status.HTTP_410_GONE, "artifact content is unavailable")
    content = await asyncio.to_thread(
        read_artifact_content, config.artifact_root, work_item_id, artifact.object_key
    )
    if content is None or not await asyncio.to_thread(
        artifact_content_matches, artifact.content_type, content,
    ):
        raise HTTPException(status.HTTP_410_GONE, "artifact content is unavailable")
    return Response(
        content=content,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": artifact_disposition(artifact.name),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@app.post("/api/runs/{work_item_id}/preview", response_model=PreviewView)
async def register_preview(
    work_item_id: str,
    payload: PreviewCreate,
    session: SessionDep,
    config: Annotated[Settings, Depends(get_settings)],
    lease_token: Annotated[str | None, Header(alias="X-Kelpie-Lease")] = None,
) -> PreviewEndpoint:
    await validate_lease(session, work_item_id, lease_token, config.lease_seconds)
    await get_work_item(session, work_item_id)
    ensure_allowed_preview_target(payload.target_url, config.preview_allowed_cidrs)
    if payload.console_target_url:
        ensure_allowed_preview_target(payload.console_target_url, config.preview_allowed_cidrs)
    endpoint = (
        await session.execute(
            select(PreviewEndpoint).where(PreviewEndpoint.work_item_id == work_item_id)
        )
    ).scalar_one_or_none()
    hostname = f"{work_item_id}.{config.preview_domain}"
    expiry = datetime.now(UTC) + timedelta(seconds=payload.ttl_seconds)
    if endpoint is None:
        endpoint = PreviewEndpoint(
            work_item_id=work_item_id,
            hostname=hostname,
            target_url=payload.target_url,
            console_target_url=payload.console_target_url,
            expires_at=expiry,
        )
        session.add(endpoint)
    else:
        endpoint.target_url = payload.target_url
        endpoint.console_target_url = payload.console_target_url
        endpoint.expires_at = expiry
    console = await session.get(ConsoleLease, work_item_id)
    if console is None:
        session.add(
            ConsoleLease(
                work_item_id=work_item_id,
                holder_type="agent",
                holder="agent",
                expires_at=expiry,
            )
        )
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="preview.registered",
            message=f"Preview available at https://{hostname}",
            payload={"hostname": hostname, "console": bool(payload.console_target_url)},
        ),
    )
    await session.commit()
    return endpoint


@app.post("/api/work-items/{work_item_id}/console-lease", response_model=ConsoleLeaseView)
async def console_lease(
    request: Request,
    work_item_id: str,
    payload: ConsoleLeaseRequest,
    session: SessionDep,
    actor: ActorDep,
) -> ConsoleLease:
    item, decision = await authorized_work_with_decision(
        session, actor, work_item_id, Role.OPERATOR, lock=True,
    )
    await ensure_worker_not_quarantined(session, item)
    lease = await session.get(ConsoleLease, work_item_id, with_for_update=True)
    if lease is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "console is not registered")
    if payload.expected_version is not None and lease.version != payload.expected_version:
        raise HTTPException(status.HTTP_409_CONFLICT, "console lease version mismatch")
    before = ConsoleOwnership.capture(lease)
    if payload.action == "acquire":
        if lease.holder_type == "user" and lease.holder != actor.subject:
            raise HTTPException(status.HTTP_409_CONFLICT, "console is held by another user")
        lease.holder_type = "user"
        lease.holder = actor.subject
        message = "Agent UI input paused; console transferred to user"
    else:
        if lease.holder_type != "user" or lease.holder != actor.subject:
            raise HTTPException(status.HTTP_409_CONFLICT, "actor does not hold the console")
        lease.holder_type = "agent"
        lease.holder = "agent"
        message = "Console returned to agent"
    lease.version += 1
    lease.expires_at = datetime.now(UTC) + timedelta(minutes=15)
    await emit_event(
        session,
        work_item_id,
        EventCreate(
            event_type="console.transferred",
            source=actor.subject,
            message=message,
            payload={"holder_type": lease.holder_type, "holder": lease.holder},
        ),
    )
    record_console_audit(
        session, request, item, actor, decision, lease, before, action=payload.action,
    )
    await session.commit()
    return lease


@app.get("/internal/previews/resolve")
async def resolve_preview(
    session: SessionDep,
    _: Annotated[None, Depends(require_gateway)],
    host: str,
    console: bool = False,
) -> dict[str, str | bool]:
    match = (await session.execute(
        select(PreviewEndpoint.id, WorkItem.assigned_worker_id)
        .join(WorkItem, PreviewEndpoint.work_item_id == WorkItem.id)
        .where(PreviewEndpoint.hostname == host.lower())
    )).one_or_none()
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "preview not found")
    # Serialize new resolutions with quarantine before reading endpoint/console state.
    worker = (
        await session.get(WorkerHost, match.assigned_worker_id, with_for_update=True)
        if match.assigned_worker_id else None
    )
    if worker is None or worker.quarantined_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "preview unavailable")
    endpoint = await session.get(PreviewEndpoint, match.id, populate_existing=True)
    if endpoint is None:
        raise HTTPException(status.HTTP_410_GONE, "preview unavailable")
    expires_at = endpoint.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "preview expired")
    target = endpoint.console_target_url if console else endpoint.target_url
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "console not available")
    read_only = True
    if console:
        lease = await session.get(ConsoleLease, endpoint.work_item_id)
        if lease is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "console lease is unavailable")
        read_only = lease.holder_type != "user"
    return {
        "target_url": target,
        "work_item_id": endpoint.work_item_id,
        "read_only": read_only,
    }
