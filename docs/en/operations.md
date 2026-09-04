# Operations

[한국어](../ko/operations.md) | English

## Local control-plane smoke test

1. Copy `.env.example` to `.env` and replace all development secrets.
2. Run `docker compose --profile demo up --build`.
3. Submit a work item from the dashboard at `http://localhost:3000`.
4. Watch the run reach `awaiting_approval`, approve it, and confirm it reaches `completed`.

The `demo` Compose profile already starts the mock worker. It deliberately does not make GitHub writes.

## Database migrations

In Compose deployments, the one-shot `api-migrate` service must complete `alembic upgrade head` before the API starts. Outside Compose, run the following command from a deployment environment using the same `DATABASE_URL` before rolling out the new API version. The API container image can run `alembic upgrade head` directly.

```bash
make migrate-api
```

Migrations acquire a PostgreSQL transaction advisory lock, so concurrent deployments change the schema one at a time. A failed migration stops the API rollout; after correcting the cause, rerun the same command. `/healthz` reports process liveness, while `/readyz` verifies database connectivity and that the database revision matches the Alembic head.

A database created by runtime `create_all` before migrations were introduced is adopted on its first upgrade without changing its data when all current tables and columns match the baseline. The migration fails if tables are missing or columns differ, and the schema must be repaired first. Use `DATABASE_SCHEMA_MODE=bootstrap` only for an empty, disposable development database; production environments must retain the default `validate` mode.

Downgrading below the current baseline is explicitly blocked because it would delete all data. Before rolling back a future revision, back up PostgreSQL and the object store, then run `alembic downgrade <revision>` only within the safe range documented by that revision. Recover incidents that require returning below the baseline by restoring a verified backup into a new database.

## GitHub App delivery

Create and install a GitHub App with repository metadata read access and Issues, Contents, and Pull requests read/write access. Configure its webhook URL as `https://<control-host>/webhooks/github`, subscribe to Issues events, and use the same random value for the App webhook secret and `GITHUB_WEBHOOK_SECRET`. Then expose these only to the API process:

```dotenv
GITHUB_APP_ID=<numeric app id>
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem
AGENT_TRIGGER_LABEL=agent-ready
```

Mount the PEM at the configured path with read permission only for the API user. Installing the App on a repository lets direct web requests resolve its installation automatically. GitHub issue events supply the installation ID in the signed webhook. Applying `agent-ready` queues the issue; the delivery token is not minted until a human approves the verified patch.

## Production control plane

- Put the API and dashboard behind an OIDC-aware reverse proxy. Set `AUTH_MODE=trusted_headers` and prevent direct network access to the API so clients cannot forge identity headers.
- Use a managed PostgreSQL service or encrypted volumes with point-in-time recovery. Run Alembic migrations before the API rollout and verify that `/readyz` succeeds.
- Store worker, GitHub, Slack, object-store, DNS, and OIDC credentials in a secret manager. Never put them in Compose files or task VM images.
- Terminate wildcard TLS at a dedicated preview gateway. Require application authentication before resolving a run target.
- Set `PREVIEW_ALLOWED_CIDRS` to the dedicated WireGuard/libvirt VM subnet. Never include control-plane, metadata, or general private-service networks.
- Retain task VMs for 24 hours and artifacts for 30 days. A scheduled janitor must verify the work item is not active and then delete explicit, UUID-named volumes only.

## KVM worker

Build the Go binary on the target architecture and copy it to `/usr/local/bin/kelpie-worker`. Run `infra/host/install-ubuntu.sh` from the repository root on a dedicated Ubuntu host. Create `/etc/kelpie/worker.env` with mode `0600`:

```dotenv
KELPIE_CONTROL_URL=https://control.example.com
KELPIE_WORKER_TOKEN=<random 32+ character value>
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
