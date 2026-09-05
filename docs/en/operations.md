# Operations

[한국어](../ko/operations.md) | English

## Local control-plane smoke test

1. Copy `.env.example` to `.env` and replace all development secrets.
2. Run `docker compose --profile demo up --build`.
3. Submit a work item from the dashboard at `http://localhost:3000`.
4. Watch the run reach `awaiting_approval`, approve it, and confirm it reaches `completed`.

The `demo` Compose profile already starts the mock worker. It deliberately does not make GitHub writes.

## Worker resource-report ordering

The Worker serializes each heartbeat snapshot and request, claim and local reservation, and release and local return under the same process lock. An old heartbeat therefore cannot overwrite available resources just after an API release and delay the next task until the periodic heartbeat. Execution itself and ordinary work events do not hold this lock.

A successful release returns the local reservation for that work exactly once. If status lookup, terminal transition, or release cannot be confirmed after execution fails, the reservation is retained and execution slots may remain unavailable. Check API connectivity, lease state, and actual VM liveness before recovery. Do not restart the Worker or manually edit database capacity merely to clear reservations.

This ordering guarantee covers concurrent requests within one Worker process only. Restart recovery, multiple daemons sharing an identity, claims with lost responses, and confirmed VM termination or retained-disk reclamation are separate lifecycle work. An API release response does not prove physical VM deletion.

API formats, database schema, and environment variables are unchanged. To apply the update, stop new assignments to the affected Worker and safely settle active work before replacing its binary. Rollback can restore the previous binary under the same conditions, but reintroduces the resource-report race; prefer correcting the cause and rolling forward.

## Database migrations

In Compose deployments, the one-shot `api-migrate` service must complete `alembic upgrade head` before the API starts. Outside Compose, run the following command from a deployment environment using the same `DATABASE_URL` before rolling out the new API version. The API container image can run `alembic upgrade head` directly.

```bash
make migrate-api
```

Migrations acquire a PostgreSQL transaction advisory lock, so concurrent deployments change the schema one at a time. A failed migration stops the API rollout; after correcting the cause, rerun the same command. `/healthz` reports process liveness, while `/readyz` verifies database connectivity and that the database revision matches the Alembic head.

Readiness inspection applies a combined two-second deadline to pool checkout, connection checks, and schema lookup. A timeout uses the existing `503 {"status":"not_ready","database_schema":"unreachable"}` response without database addresses, credentials, or raw exceptions. Even when the database is unresponsive at startup, this check completes and process liveness becomes observable through `/healthz`. This is not a timeout change for ordinary application queries or entire migrations. See [failure/recovery verification and operational limitations](readiness-verification.md).

A database created by runtime `create_all` before migrations were introduced is adopted on its first upgrade without changing its data when all current tables and columns match the baseline. The migration fails if tables are missing or columns differ, and the schema must be repaired first. Use `DATABASE_SCHEMA_MODE=bootstrap` only for an empty, disposable development database; production environments must retain the default `validate` mode.

Downgrading below the current baseline is explicitly blocked because it would delete all data. Before rolling back a future revision, back up PostgreSQL and the object store, then run `alembic downgrade <revision>` only within the safe range documented by that revision. Recover incidents that require returning below the baseline by restoring a verified backup into a new database.

Revision `20260904_0002` backfills existing work items and events by using the work-item ID as their correlation ID. Downgrading to `20260904_0001` removes only the new correlation columns and indexes and retains existing work data. To roll back, stop API traffic or switch to the previous API version that does not require the correlation fields before downgrading the migration.

Revision `20260904_0003` adds tables for one-time OIDC login attempts and opaque authentication sessions. Downgrading to `20260904_0002` removes both tables and active login sessions while preserving work-item data. Switch to the previous API version before the downgrade and inform users that they must sign in again.

Revision `20260905_0004` adds organization, principal, membership, repository, grant, and Slack identity tables plus `organization_id` on work items. All historical work is assigned to the `legacy` organization, which has no identity binding or members, and is hidden from ordinary user lists and detail routes. Registering the same repository name does not transfer historical ownership. Work, event, and artifact data are preserved; reassignment of historical work requires separately reviewed follow-up work. Existing worker leases retain their contract.

Before rolling back RBAC, stop API ingress, webhooks, and workers and back up the database and policy files. `alembic downgrade 20260904_0003` preserves work data but removes organization boundaries and authorization tables. Do not re-expose the previous API to multiple organizations. Run it only in an isolated maintenance environment and restore validated backups and policies after recovering the RBAC version.

## Startup delivery recovery

An API started with an unready database [resumes pending deliveries](delivery-recovery.md) after connectivity/schema recovery. This recovery supports one API process only: fully stop the previous API before starting its replacement. Do not use a 200 from `/readyz` as a delivery-completion signal.

## OIDC authentication

Follow the [secret management guide](secret-management.md) for file injection, rotation, and recovery. First complete [per-worker credential issuance and migration](worker-credentials.md) for Workers.

Serve the dashboard and the API `/auth` and `/api` paths from the same public HTTPS origin in production. Dashboard Server Components forward the browser session cookie to the internal API, and browser requests and event streams include credentials. Do not expose the API on a separate public hostname or inject OIDC identity headers.

Register an Authorization Code client with the identity provider and set its callback URI to `https://<control-host>/auth/callback`. PKCE S256 is always used, regardless of the client type. Configure a client secret unless the provider advertises `none` authentication for a public client in its metadata. The organization claim must be a non-empty scalar string.

```dotenv
AUTH_MODE=oidc
OIDC_ISSUER_URL=https://identity.example.com
OIDC_CLIENT_ID=kelpie-control
OIDC_CLIENT_SECRET=<inject from secret manager>
OIDC_REDIRECT_URI=https://control.example.com/auth/callback
OIDC_ORGANIZATION_CLAIM=organization
OIDC_SCOPES=openid,profile
OIDC_ALLOWED_ALGORITHMS=RS256
DASHBOARD_URL=https://control.example.com
```

The issuer, redirect URI, and discovered authorization, token, and JWKS endpoints must use HTTPS. The discovered issuer must exactly match configuration. Kelpie validates the ID-token signature, explicit algorithm allowlist, audience, expiry, issued-at time, nonce, authorized party, and organization claim. The allowlist cannot contain `none` or symmetric `HS*` algorithms.

Start login at `/auth/login`. The `state`, `nonce`, and PKCE verifier are held in a one-time database record for five minutes. After authentication the browser receives only a random opaque token in a `Secure`, `HttpOnly`, `SameSite=Strict` cookie, while the database stores its SHA-256 hash. Session lifetime defaults to the earlier of eight hours or ID-token expiry. `/auth/logout` removes both the server session and cookie.

The `trusted_headers` authentication mode has been removed. `X-Kelpie-User` and `X-Kelpie-Role` are not authentication inputs. Development mode also ignores request headers and uses only the fixed administrator identity from `DEVELOPMENT_SUBJECT` and `DEVELOPMENT_ORGANIZATION`; never expose it publicly.

OIDC login requires a registered organization and membership. Organizations are identified by `(issuer, organization claim)` and principals by `(issuer, subject)`; arbitrary role claims in ID tokens are not used. Sessions and event streams recheck membership and authorization, so revocation applies on the next request or event query. Cookie-authenticated work mutations require an `Origin` header matching the origin of `DASHBOARD_URL`.

The preview gateway returns 503 in its default `disabled` mode until scoped OIDC preview grants are implemented. Use `KELPIE_GATEWAY_AUTH_MODE=development` only for an isolated local demo.

## Organization and repository authorization

After migration and before opening login traffic, a control-host administrator copies the [policy example](../../config/iam.example.json) and supplies the actual issuer, organization claim, subjects, repositories, GitHub App installation IDs, and Slack team/user IDs. A policy file represents the **entire desired state** of one organization. Keep it on an access-controlled control host without tokens or client secrets.

Run the command inside the API image as an administrative process using the same `DATABASE_URL`. From the repository root with the local virtual environment, use `.venv/bin/python -m app.iam /path/to/organization.json`.

```bash
python -m app.iam /run/config/organization.json
```

The command replaces the organization's memberships, repository grants, Slack bindings, and registered repositories in one transaction. Omitted entries are revoked, so always submit the complete policy. At least one administrator is required. Reassigning the organization identity, claiming another organization's registered repository or Slack binding, and granting access to nonmembers are rejected. Failed applications roll back completely. Policy administration requires control-host operational access; there is no public bootstrap or permission-management API.

| Effective role | Read, events, artifacts | Create, feedback, console takeover | PR, budget, console approval | Cancel unassigned queued work |
| --- | --- | --- | --- | --- |
| Viewer | Allowed | Denied | Denied | Denied |
| Operator | Allowed | Allowed | Denied | Denied |
| Approver | Allowed | Allowed | Allowed | Denied |
| Administrator | Allowed | Allowed | Allowed | Allowed |

Organization membership sets the baseline role across all registered repositories in that organization. Repository grants can elevate that role only for the named repository and cannot lower the baseline. No role applies across organizations. `/auth/session` returns the internal organization ID in `organization` and the organization baseline in `role`, excluding repository-specific elevation. Unknown repositories and other organizations' work return 404; insufficient roles within the same organization and unregistered memberships return 403. Work-item response shapes and worker lease contracts remain unchanged, while new repository names are normalized to lowercase.

GitHub webhooks require a valid signature and matching registered repository and installation ID before creating work. Web submissions in OIDC mode also use the policy's installation ID. Slack commands resolve the signed `(team_id, user_id)` binding to a principal and use the same authorization checks; `SLACK_APPROVER_USER_IDS` no longer grants approval rights. Slack work records use the linked principal ID as actor. The global Slack notification channel remains deployment-wide, so enable notifications only where every channel participant may view the transmitted work information.

Isolated development mode auto-registers its dedicated organization and repository on direct work submission. The development organization cannot overlap an OIDC organization or `legacy`. Organization/repository authorization and append-only feedback, console, approval, and [unassigned queued cancellation](work-cancellation.md) audits are implemented. Active-run administrator cancellation and delivery auditing remain IAM/OPS follow-up scope.

## Observability and correlation

The API returns UUID-formatted `X-Request-ID` and `X-Kelpie-Correlation-ID` headers on every response. Valid incoming UUID values are preserved; invalid values are replaced. The correlation ID selected on the initial request is persisted on the work item and events and propagated through the worker, VM runner, web feedback, Slack status metadata, and background delivery. It is used only for tracing, never for authentication or authorization.

Prometheus can scrape the following low-cardinality metrics from `GET /metrics`:

- HTTP request count and duration by method, route template, and status
- Worker claim outcomes and queue latency
- Work-state transition count and duration in each state
- Approval decisions, initial and retried delivery attempts, and delivery outcomes
- [Startup delivery recovery](delivery-recovery-metrics.md) phase, elapsed time including waits, and readiness/recovery iteration outcomes

Work IDs, repository names, users, and correlation IDs are never metric labels. Do not expose `/metrics` publicly; restrict it to the internal Prometheus network with a reverse proxy or network policy.

Logs use JSON by default and every request log includes the request and correlation IDs. Set the full trace ingestion URL when using an OTLP HTTP collector.

```dotenv
LOG_FORMAT=json
OTEL_SERVICE_NAME=kelpie-api
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
```

When the OTLP endpoint is empty, application spans are not exported, while Prometheus metrics and structured logs remain available. [Baseline alerts and runbooks](monitoring-alerts.md) cover failed/missing scrapes, delayed startup recovery, and observed delivery failures. External object-store, SCM, and delivery-worker readiness, worker/lease alerts, and an integrated operations dashboard remain follow-up OBS-001 scope.

## GitHub App delivery

Diagnose delivery failures with stages and error codes instead of raw exceptions. See [safe delivery-failure diagnostics](delivery-failure-safety.md) for the event contract, historical-record handling, and verification evidence.

Create and install a GitHub App with repository metadata read access and Issues, Contents, and Pull requests read/write access. Configure its webhook URL as `https://<control-host>/webhooks/github`, subscribe to Issues events, and use the same random value for the App webhook secret and `GITHUB_WEBHOOK_SECRET`. Then expose these only to the API process:

```dotenv
GITHUB_APP_ID=<numeric app id>
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem
AGENT_TRIGGER_LABEL=agent-ready
```

Mount the PEM at the configured path with read permission only for the API user. OIDC mode requires the repository's App installation ID in the organization policy; only direct development-mode requests discover installation metadata automatically. GitHub issues carrying `agent-ready` are queued only when the signature and registered installation ID match. Delivery tokens are not minted until an authorized user approves the verified patch.

## Production control plane

- Serve the API and dashboard from the same public HTTPS origin and use `AUTH_MODE=oidc`. The reverse proxy must not create identity headers, and direct network access to the internal API address must be blocked.
- Use a managed PostgreSQL service or encrypted volumes with point-in-time recovery. Run Alembic migrations before the API rollout and verify that `/readyz` succeeds.
- Store worker, GitHub, Slack, object-store, DNS, and OIDC credentials in a secret manager. Never put them in Compose files or task VM images.
- Do not expose the preview gateway until scoped OIDC preview grants are implemented. After that, terminate wildcard TLS at a dedicated gateway and validate the grant before resolving a run target.
- Set `PREVIEW_ALLOWED_CIDRS` to the dedicated WireGuard/libvirt VM subnet. Never include control-plane, metadata, or general private-service networks.
- Retain task VMs for 24 hours and artifacts for 30 days. A scheduled janitor must verify the work item is not active and then delete explicit, UUID-named volumes only.

## KVM worker

Build the Go binary on the target architecture and copy it to `/usr/local/bin/kelpie-worker`. Run `infra/host/install-ubuntu.sh` from the repository root on a dedicated Ubuntu host. Create `/etc/kelpie/worker.env` with mode `0600`:

```dotenv
KELPIE_CONTROL_URL=https://control.example.com
KELPIE_WORKER_TOKEN_FILE=/run/secrets/kelpie/worker-1.token
KELPIE_WORKER_NAME=worker-1
KELPIE_EXECUTOR=libvirt
KELPIE_CPU_TOTAL=16
KELPIE_MEMORY_MB_TOTAL=49152
KELPIE_DISK_GB_TOTAL=500
KELPIE_BASE_IMAGE=/var/lib/kelpie/images/ubuntu-desktop.qcow2
KELPIE_WORK_ROOT=/var/lib/kelpie/runs
```

The golden image must have a `kelpie` user, Codex, `kelpie-runner`, Git, language toolchains, a desktop/browser stack, qemu-guest-agent, and the runner systemd unit. Perform ChatGPT device login on a sealed template, copy its credential material into per-VM tmpfs at boot, and ensure it is not present in screenshots, cloud-init logs, or retained artifacts.

## Incident controls

- Drain a worker by changing its state before maintenance; do not terminate running VMs blindly.
- Rotate the worker secret if a host is compromised, revoke its WireGuard peer, and invalidate active leases assigned to it.
- A leaked task lease has access to only one run. Mark that lease revoked and preserve its events for investigation.
- Treat unexpected outbound traffic, attempts to reach host services, or secret-like strings in event payloads as security incidents.
