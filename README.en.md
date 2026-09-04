# Kelpie

[한국어](README.ko.md) | English

Kelpie is a self-hosted control plane for autonomous software-development agents. It turns GitHub issues or direct requirements into isolated jobs, streams every action to a dashboard, collects human feedback, and gates commits and pull requests behind explicit approval.

The repository contains a working first vertical slice:

- `apps/api`: FastAPI control plane with a durable PostgreSQL state machine, GitHub App ingestion/delivery, worker leases, SSE events, Slack feedback, RBAC, and approval gates.
- `apps/web`: Next.js operations dashboard with Korean and English locale routes, live job state, and event streaming.
- `apps/worker`: Go host daemon with resource reporting, work claiming, and mock/libvirt executor boundaries.
- `apps/runner`: VM-side Codex App Server adapter using the stable stdio JSON-RPC transport.
- `apps/gateway`: authenticated wildcard preview and console reverse proxy with exclusive console leases.
- `infra`: Ubuntu/KVM host bootstrap, egress policy, and systemd units.

## Quick start

```bash
cp .env.example .env
docker compose --profile demo up --build
```

Open <http://localhost:3000>. The browser language is detected on the first visit, and the dashboard can be switched between `/ko` and `/en`. Development authentication treats requests as an administrator. Never use `AUTH_MODE=development` on a public deployment.

Create a direct request:

```bash
curl -X POST http://localhost:8000/api/work-items \
  -H 'content-type: application/json' \
  -d '{"title":"Add a health endpoint","requirement":"Implement and test GET /health","repository":"owner/repo"}'
```

Run all locally available checks with `make test`. Read the [architecture](docs/en/architecture.md), [operations](docs/en/operations.md), and [security model](docs/en/security.md) before deploying a KVM worker.

The demo profile uses a mock executor so the complete queue → approval → completion lifecycle can be exercised without Linux/KVM or GitHub credentials. Real runs require an Ubuntu KVM worker and a GitHub App. After independent verification, the VM uploads a binary Git patch; only the central control plane can mint the installation token, commit the patch, push the deterministic agent branch, and create the pull request.

This is the first deployable vertical slice, not the final autonomous-security-scanner product. The [roadmap summary](docs/en/roadmap-summary.md) and [detailed plan](docs/en/roadmap-detailed.md) define the remaining production work.

## Security boundary

An agent receives root access inside its task VM, never on the worker host. Worker credentials and repository write tokens must not be injected into a VM. A per-run lease authorizes only event and state updates; write credentials are minted only after an approver accepts the proposed change.
