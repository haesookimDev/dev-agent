# Detailed development plan

[한국어](../ko/roadmap-detailed.md) | English · [Summary](roadmap-summary.md)

## 1. Planning rules

- Preserve the trust boundary: task VMs are disposable and untrusted; only the control plane can approve or mint write credentials.
- Ship vertical capabilities with tests and operational controls, rather than adding provider-specific shortcuts to the API.
- Every milestone requires unit, integration, failure-recovery, and browser/computer-use verification proportional to its risk.
- Keep provider, executor, artifact-store, identity, and messenger implementations behind explicit interfaces.
- Treat an autonomous proposal and its execution as different principals. A proposal must pass policy or human triage before it can become executable work.

Effort uses relative sizes: S is a narrow change, M is a multi-component feature, and L is an infrastructure or security milestone requiring a dedicated test environment.

## 2. P0 — production foundation

### MIG-001 · Versioned database migrations — M

Scope:

- Introduce Alembic with a baseline migration for every current model.
- Disable runtime `create_all` outside test and explicit bootstrap modes.
- Add migration locking and a deployment command that runs before API rollout.
- Test an empty install, upgrade from the baseline, rollback where safe, and restart during migration failure.

Done when a production-like deployment upgrades without data loss and a schema mismatch prevents the API from becoming ready.

### IAM-001 · OIDC identity and repository authorization — L

Scope:

- Validate issuer, audience, signature, expiry, nonce, and organization claims.
- Model organization roles and repository-level viewer, operator, approver, and administrator grants.
- Replace development headers at the public boundary; trusted identity headers may only arrive from the internal reverse proxy.
- Record actor ID, identity provider, role decision, request ID, and source IP for feedback, console takeover, approval, cancellation, and delivery.

Done when cross-organization access, forged headers, expired tokens, and viewer approvals fail in integration tests.

### SEC-001 · Secret provider and credential rotation — M

Scope:

- Add a secret-provider interface with file/Kubernetes/Vault implementations as deployment needs dictate.
- Replace the global Worker secret with individually identified, revocable Worker credentials or mTLS certificates.
- Define rotation without stopping active runs and revoke credentials on Worker quarantine.
- Ensure logs, events, artifacts, crash dumps, and cloud-init output pass secret scanning.

Done when a single Worker can be revoked without rotating every Worker and no secret appears in retained evidence.

### OBS-001 · Observability and correlation — M

Scope:

- Propagate a correlation ID through webhook, work item, claim, VM, Codex turn, artifact, notification, and delivery.
- Export OpenTelemetry traces and Prometheus metrics for queue latency, provisioning time, run duration, retries, token/cost estimates, approvals, and failures.
- Add structured health/readiness checks for database, object store, SCM, and background delivery workers.
- Define alert thresholds and dashboard panels for stuck states, lease expiry, Worker heartbeat loss, and delivery errors.

Done when one run can be traced end to end and an intentionally stuck run produces an actionable alert.

### OPS-001 · Retention and recovery workers — M

Scope:

- Implement explicit retention policies for events, artifacts, delivery bundles, VM disks, previews, and audit records.
- Add idempotent janitor jobs using validated UUID targets and active-run safety checks.
- Document and test PostgreSQL backup/restore and object-store recovery.
- Add administrative quarantine, retry, cancel, and force-release operations with audit events.

Done when expired data is removed without touching active work and a backup is restored into a clean environment.

## 3. P1 — real KVM execution and connectivity

### IMG-001 · Reproducible golden-image pipeline — L

- Build pinned Ubuntu desktop images with Packer or an equivalent declarative builder.
- Install Codex, Runner, browser, desktop, qemu-guest-agent, common toolchains, CA roots, and update policy.
- Produce an SBOM, vulnerability report, checksum, signature, and image version metadata.
- Boot-test every image and roll back Worker rollout automatically when the health probe fails.

### KVM-001 · Complete VM lifecycle — L

- Track domain, overlay, seed, IP, boot state, timestamps, and cleanup state in durable run metadata.
- Add boot timeout, graceful shutdown, forced termination, orphan reconciliation, and host-restart recovery.
- Allocate CPU, memory, disk IOPS, and concurrent browser capacity; return resources exactly once.
- Use per-run networks and deny direct access to the host, metadata endpoints, and other VMs.

### NET-001 · WireGuard preview network — L

- Establish outbound Worker peers and per-run routable addresses without opening inbound Worker ports.
- Automate wildcard DNS and certificates at the Preview Gateway.
- Authorize every HTTP/WebSocket resolution against user, organization, run, target CIDR, and expiry.
- Test host-header injection, rebinding, redirects to forbidden networks, and stale preview routes.

### GUI-001 · Enforced console ownership — M

- Put a hardened input filter in front of noVNC so read-only state is enforced, not merely communicated by a header.
- Pause agent mouse/keyboard actions before granting user ownership and require lease version checks on hand-back.
- Record takeover, input pause, return, timeout, and forced recovery events.
- Prove two concurrent GUI jobs cannot share a display, clipboard, browser profile, or input channel.

### E2E-001 · Physical-host acceptance suite — M

Run two unrelated repositories concurrently through clone, Codex analysis, browser verification, feedback, re-verification, approval, delivery, and cleanup. Inject a Worker restart and a network interruption. The suite passes only if both runs recover without credential leakage, cross-run input, duplicate PRs, or leaked resources.

## 4. P2 — verification and evidence

### VER-001 · Repository verification policy — M

- Define a versioned repository policy describing required commands, allowed overrides, timeouts, environment services, browser journeys, and evidence requirements.
- Detect changed areas and select relevant tests while retaining repository-required gates.
- Record command, exit code, duration, environment fingerprint, output digest, and retry reason.
- Distinguish deterministic failures, infrastructure failures, flaky results, and policy failures.

### BROWSER-001 · Browser/computer-use supervisor — L

- Allocate a unique browser profile and display per run.
- Drive declared acceptance journeys and capture screenshots at stable checkpoints.
- Redact configured selectors and secret-shaped values before upload.
- Reject approval when required UI evidence is absent, stale, from another run, or captured before the final code revision.

### ART-001 · Production artifact store — M

- Replace local artifact paths with an object-store interface, content hashes, encryption, retention tags, and short-lived signed downloads.
- Add malware/content scanning, quota enforcement, multipart upload, and immutable evidence manifests.
- Keep binaries private by default and log every download.

### EVAL-001 · Independent result evaluator — M

- Run a separate evaluator principal after implementation.
- Compare requirement acceptance criteria, code diff, test results, browser evidence, and policy.
- Produce structured pass/fail findings and block approval on unresolved high-severity findings.
- Prevent the implementing agent from changing evaluator policy or result records.

## 5. P3 — providers and messaging

### SCM-001 · SCM provider contract — M

Define provider-neutral repository, installation, issue, branch, commit, check, comment, and merge-request operations. Move GitHub-specific fields into provider-owned metadata without breaking current work items.

### GL-001 · GitLab adapter — L

Implement signed System Hooks, label triggers, project access tokens or OAuth application credentials, branch/MR delivery, idempotency, comments, approvals, and GitLab status checks. Reuse the same delivery bundle and approval boundary.

### SCM-002 · Review synchronization — M

Import PR/MR review comments as feedback, correlate them to a work item, rerun implementation and verification, and update checks. Never treat a comment as approval unless provider identity maps to an authorized approver.

### MSG-001 · Messenger delivery — M

Create a messenger interface for Slack first and additional providers later. Send localized summaries, result images, evidence links, approval controls, and failure diagnostics. Verify signatures and map messenger identities to platform identities before accepting feedback or approval.

## 6. P4 — scheduling and multi-agent orchestration

### ROUTE-001 · Resource- and duration-aware scheduler — L

- Collect per-repository and per-task-class history for queue, setup, model, test, browser, and delivery time.
- Estimate CPU, memory, disk, accelerator, browser/display, network, and wall-time demand with confidence bounds.
- Score capable Workers using fit, cache locality, predicted completion time, fragmentation, fairness, failure rate, and maintenance state.
- Reserve resources centrally before assignment and reconcile them against Worker-local reservations.
- Define SLOs for queue latency and starvation; use aging so large or uncommon jobs eventually run.

### DAG-001 · Main-agent and sub-agent work graph — L

- Represent analysis, implementation, review, testing, and investigation as a durable DAG.
- Let the main agent create bounded child tasks with repository snapshots, explicit capabilities, budgets, and acceptance criteria.
- Isolate child worktrees and merge results only through the main agent after conflict and test checks.
- Stream graph state and per-agent logs to the dashboard.

### REC-001 · Checkpoints and recovery — M

Persist Codex thread IDs, repository revision, worktree diff, verification manifest, artifact manifest, budget use, and pending questions. Resume only when the base revision and policy remain compatible; otherwise replan explicitly.

### COST-001 · Budget policy — M

Track model tokens, compute minutes, storage, and external service costs. Support per-run, repository, and organization limits. Crossing a soft limit requests approval; crossing a hard limit pauses work without losing the checkpoint.

## 7. P5 — autonomous discovery

### DISC-001 · Discovery scheduler — L

Run read-only scheduled jobs for dependency updates, SAST, secret scanning, test gaps, flaky tests, performance regressions, dead code, and documentation drift. Discovery credentials must not modify code or apply execution labels.

### TRIAGE-001 · Finding normalization and deduplication — M

Normalize tool findings into fingerprints, affected revisions, severity, confidence, exploitability, ownership, and evidence. Suppress duplicates and reopen only when a finding recurs on a relevant revision.

### ISSUE-001 · Governed issue creation — M

Generate a proposed issue with evidence, impact, remediation direction, acceptance criteria, and risk. Apply a proposal-only label. A separate policy engine or authorized human must promote it to the executable label.

### SAFE-001 · Autonomous safety evaluation — L

Maintain adversarial scenarios for prompt injection, poisoned repositories, malicious tests, exfiltration attempts, self-approval, scope expansion, destructive cleanup, and false-positive issue storms. Autonomous operation remains disabled until these evaluations pass at the required rate.

## 8. P6 — scale and release operations

- HA control-plane processes with singleton-safe background jobs and PostgreSQL advisory locking.
- Multi-tenant quotas, network separation, encryption keys, audit export, and data residency controls.
- Canary Worker and golden-image rollout, compatibility matrices, and rollback automation.
- Capacity planning, chargeback reports, SLO/error budgets, incident runbooks, and disaster-recovery exercises.

## 9. Definition of done for every issue

1. Acceptance criteria and threat considerations are recorded before implementation.
2. Unit and integration tests cover success, authorization failure, timeout, retry, and duplicate delivery where applicable.
3. User-facing UI text is added to both Korean and English catalogs.
4. Operator documentation is updated in both languages.
5. Logs and evidence contain correlation IDs and pass redaction checks.
6. Browser/computer-use verification is attached for user-visible changes.
7. Migration, rollback, compatibility, and cleanup behavior are documented.
8. No commit, push, PR/MR, budget increase, or console takeover bypasses its approval policy.

## 10. First implementation batch

Create the first batch as separate issues in this dependency order:

1. `MIG-001` database migrations.
2. `OBS-001` correlation IDs and baseline metrics.
3. `IAM-001` OIDC and authorization model.
4. `SEC-001` per-Worker credentials and secret provider.
5. `IMG-001` reproducible VM image.
6. `KVM-001` lifecycle recovery.
7. `NET-001` WireGuard previews.
8. `GUI-001` enforced console ownership.
9. `E2E-001` concurrent physical-host acceptance.

Do not start autonomous discovery until this batch and the P2 evidence gates are complete.
