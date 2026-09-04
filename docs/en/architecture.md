# Architecture

[한국어](../ko/architecture.md) | English

Kelpie separates control authority from untrusted development execution.

```text
GitHub / Web / Slack
          │ HTTPS webhooks and feedback
          ▼
┌──────────────── central VPS ────────────────┐
│ Next.js UI ─ FastAPI ─ PostgreSQL           │
│                    └ events / leases        │
└──────────────────────┬──────────────────────┘
                       │ outbound WireGuard + TLS
                       ▼
┌──────────── dedicated Ubuntu worker ────────┐
│ Go daemon ─ libvirt/QEMU/KVM                │
│               ├ task VM A: Codex + browser │
│               └ task VM B: Codex + browser │
└─────────────────────────────────────────────┘
```

## Trust boundaries

The control plane owns identities, approvals, repository installations, and write credentials. The worker owns VM lifecycle but only receives a global credential for worker registration and claims. A task VM receives a random, hashed, renewable lease scoped to one work item. The lease can append events, read feedback, and request valid state transitions; it cannot claim work, read another job, mint a GitHub token, or approve itself.

Issue text and repository contents are untrusted input. They run only inside the task VM. Agents have root in that VM but never receive a host socket, host mount, worker credential, or direct route to private networks. Internet egress is logged and RFC1918, link-local metadata, and control-plane administrative addresses are denied.

## Durable state

PostgreSQL is the source of truth. Each work item has a monotonically increasing version. A transition supplies its expected version and is committed in the same transaction as its event. This turns duplicate webhooks, retries, worker restarts, and concurrent feedback into either one successful transition or an explicit `409` conflict.

The initial worker transport is outbound HTTPS polling. The API types deliberately isolate transport DTOs from domain types so it can be replaced by an mTLS gRPC stream without changing work-item semantics.

## Agent adapter

The VM runner starts `codex app-server` as a child process and communicates over newline-delimited JSON-RPC on stdio. It records thread, turn, item, command, and tool notifications as normalized events. Approval requests for operations inside the isolated VM can be accepted for the session; commits, pushes, pull requests, budget extensions, and console takeover remain platform-level approvals that the agent cannot answer.

Verification evidence written under `.kelpie/artifacts` is filtered for supported image/text formats, bounded to 10 MiB per file, uploaded with the run lease, and exposed in the dashboard. Images are also sent through Slack's external-upload flow when Slack is configured. Artifact names, MIME signatures, paths, and symlinks are validated before content leaves the VM or is served by the control plane.

No model name is hard-coded. The Codex installation determines its configured default, which allows controlled upgrades independently of the control-plane release.

## Controlled delivery

The VM never receives a repository write token. After checks pass it uploads a bounded binary Git patch to the control plane and enters `awaiting_approval`. An approver can inspect the run and accept or return feedback. Approval creates a durable delivery job; the control plane mints a short-lived GitHub App installation token, reapplies the patch to a fresh default-branch clone, commits as the bot, pushes an `agent/<work-id>-<slug>` branch, and opens the PR. Deterministic branches plus GitHub branch/PR lookups allow an interrupted delivery to resume without creating duplicate PRs.

## Preview and console routing

The runner can register an expiring preview target and optional noVNC target. Targets must be literal addresses inside `PREVIEW_ALLOWED_CIDRS`, preventing the gateway from becoming an arbitrary SSRF proxy. A wildcard gateway resolves the hostname through the control plane and proxies HTTP/WebSocket traffic. Console ownership is an optimistic, versioned lease. The agent owns input by default; a user takeover is exclusive and audited. The current gateway communicates the read-only state to the console upstream, so the noVNC deployment must enforce that signal at its input boundary.

## Current boundary

The current vertical slice implements GitHub issue/direct-request ingestion, durable orchestration, mock and libvirt executor boundaries, Codex execution, independent command verification, live observation, web/Slack feedback, controlled GitHub delivery, wildcard preview routing, versioned database migrations, end-to-end correlation IDs, baseline traces, metrics, and structured logs, and OIDC Authorization Code authentication. Production deployments still need organization/repository authorization and immutable audit records, external dependency readiness and alerts, scoped OIDC preview grants, a real WireGuard path to VM previews, a hardened noVNC input filter, automatic golden-image builds, and retention workers. GitLab delivery and autonomous issue discovery are provider adapters planned after this slice.
