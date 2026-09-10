# MVP progress — 2026-09-10

[한국어](../ko/mvp-progress.md) | English · [Summary](roadmap-summary.md) · [Detailed criteria](roadmap-detailed.md)

## Baseline and percentage

Code baseline: `main` at `8cd09123cb3a2031cf18331cfe2ec74d7de1919d` (merged [PR #53](https://github.com/haesookimDev/dev-agent/pull/53)). This snapshot compares documentation with current code, tests and actual runtime evidence. Draft branches and plans do not count as complete.

Synchronized `main` is `1fd999f`, including documentation PR #54; [its CI](https://github.com/haesookimDev/dev-agent/actions/runs/34423510547) passed. [Draft PR #55](https://github.com/haesookimDev/dev-agent/pull/55) develops the [golden image candidate builder](golden-image-inputs.md). Initial input-only Head `01cf33e` passed [CI](https://github.com/haesookimDev/dev-agent/actions/runs/34425626619) in 7m53s, but that does not validate later code. Follow-up functional baseline `ce5a6f5` adds guest installation/sealing and Packer 1.16.0/QEMU plugin 1.1.6 integration; `c458cc1` adds syntax checking to existing parallel CI.

Follow-up verification passed **46 file/CLI/guest-helper tests**, `make lint` and actual Packer full configuration validation with synthetic inputs. Full `make test` against an isolated PostgreSQL database passed API **1,270/one skipped** (188.24s), Runner 45, Worker/Gateway, Web 125/type checking and image tests 46. The skip is Linux systemd syntax checking unavailable on macOS. Actual subprocess host refusal/environment filtering/timeout termination is recorded separately from simulated build failures/key cleanup. **No actual image installation/boot/desktop evidence exists; the PR remains Draft/unmerged and does not increase the completed count.** Check the PR separately for final-head CI results.

Use the seven numbered steps in [the next release](roadmap-summary.md) as a fixed denominator. **Verified complete: 1/7 = 14.3%**; partial: 3/7; incomplete: 3/7. Partial implementation receives no arbitrary fractional credit. This measures verified release stages, not code volume, effort spent or remaining schedule. Current evidence does not support a precise engineering-effort completion percentage.

## Status against completion criteria

| Stage | Assessment and inspected evidence | Remaining for completion |
| --- | --- | --- |
| 1. Versioned DB migration | **Complete.** Alembic Head `20260909_0011`, PostgreSQL migration lock, default `validate`, empty/legacy adoption, safe downgrade, failed-chain rollback and actual restore regressions. [Operations](operations.md), [migration tests](../../apps/api/tests/test_migrations.py), [atomicity](../../apps/api/tests/test_migration_atomicity.py) | Every future schema change must pass the same gates. This does not authorize arbitrary production database cutover. |
| 2. OIDC, organization/repository authorization and audit | **Partial.** OIDC/RBAC and append-only feedback/approval/queued cancellation/delivery audit exist. [Operations](operations.md), [IAM tests](../../apps/api/tests/test_iam.py), [audit](control-action-audit.md) | OIDC Preview Grant integration and actual TLS-boundary validation; running-work administrative cancellation with confirmed VM shutdown/cleanup. |
| 3. Worker secrets and quarantine | **Partial.** File provider, individual issuance/overlap rotation/revocation, control-plane quarantine and Runner/API/Worker diagnostic protection. [Credentials](worker-credentials.md), [quarantine](worker-quarantine.md), [latest diagnostics](worker-private-diagnostics.md) | Physical Host/VM/network and established-connection isolation; comprehensive event/artifact/crash-dump/cloud-init non-disclosure and retention policy. Limited redaction is not general secret scanning. |
| 4. Observation, recovery and retention foundation | **Partial.** Correlation/metrics/DB readiness/alerts, real PostgreSQL restore, ordinary-artifact backup and scheduled cleanup. [Observation](execution-monitoring.md), [restore](postgres-restore.md), [scheduler](scheduled-retention.md) | External-dependency readiness, actual-progress stall detection/operational dashboard, other data retention/janitors, physically safe running cancellation/retry/forced release, external-store and actual operational recovery verification. |
| 5. Golden image and real libvirt execution | **Incomplete.** The [executor](../../apps/worker/internal/daemon/executor.go) is an initial provisioner using a supplied base image. The [host installer](../../infra/host/install-ubuntu.sh) is not an image builder. | Reproducible version-pinned desktop image, integrity/boot checks, persistent run metadata, timeout/shutdown/restart/orphan recovery, exact resource reclamation, per-work networks and physical time-budget enforcement. |
| 6. WireGuard previews and console ownership | **Incomplete.** The [gateway](../../apps/gateway/main.go) returns 503 without production authentication and only forwards a read-only console header. | Actual WireGuard routing, wildcard TLS, organization/work/expiry/target validation, noVNC input filtering, stopping agent input, versioned return and timeout recovery. Headers do not prove input blocking. |
| 7. Real-host concurrent two-work acceptance | **Incomplete.** Current Mock/HTTP/Chromium regressions are not substitute evidence. | Two different repositories must concurrently clone→analyze→implement→browser-verify→receive feedback→reverify→approve→deliver→clean up, recovering from Worker restart/network interruption with isolated displays/input/profiles/networks/disks/credentials. |

These stages summarize detailed P0/P1 requirements; they do not waive [security invariants](security.md), approval policy or actual-use gates. Do not arbitrarily add all P2/GitLab/advanced routing/autonomous discovery to this MVP or remove unfinished P0/P1 conditions.

## Development foundation already built

- [Task-centered UI/UX](dashboard-verification.md): dashboard navigation/search/status filters/responsiveness/error recovery, followed by feedback/evidence/budget-approval improvements. This is separate from completing two real KVM jobs.
- [Development instructions](../../AGENTS.md): branch before implementation, focused Korean commits, automated and actual-use verification, evidence-backed PRs, exact-head checks, normal merge commits and `main` verification. Production deployment, paid resources and protection bypass are not authorized.
- [GitHub CI](../../.github/workflows/ci.yml): two API partitions, subsequent PostgreSQL/Runner gate, Go and Web/Chromium. Pinned action SHAs, least privilege, caching, 8-minute job limits, older-run cancellation per PR and evidence retention remain. [Partition policy](ci-partitions.md)
- [PR #53 CI](https://github.com/haesookimDev/dev-agent/actions/runs/34422241723) passed first attempt in 4m56s at exact Head `9d9b35f`: API 503/663, separate PostgreSQL groups 16/23/38/8/12/6/9, Runner 45, Web 125/Chromium 41, Go and static checks. API-stage PostgreSQL skips are covered by subsequent database checks.

Do not double-count this foundation toward the seven product release stages. The actual Worker/production-Web checks and native-focus limitation are separated in the [diagnostic verification record](worker-private-diagnostics.md).

## Next order and external dependencies

1. **Prioritize the P1 execution path:** reproducible golden image → persistent VM lifecycle/isolation → enforced preview/console boundaries → actual two-work/failure-recovery acceptance. Feature PRs lacking necessary real-environment verification remain Draft.
2. Complete remaining P0 secret/observation/retention/administrative controls in connection with that execution path. Repeated easily tested peripheral improvements do not substitute for P1 completion.
3. An approved dedicated Linux/KVM test host and necessary test network/image/TLS access conditions have not been supplied. Mac Mock/command fixtures do not prove host acceptance. Do not discover and use ambient production hosts or credentials without approval.
4. Preview work in [Draft PR #21](https://github.com/haesookimDev/dev-agent/pull/21) is not in `main`. CI on its old head does not prove integration with current migration/authorization contracts. Do not count it complete or force-merge before actual TLS/browser/console verification.

## Updating this record

For subsequent completion reports, update the baseline SHA, PR/CI/actual-use evidence and remaining conditions. Increment the numerator only when every condition of a stage is proven. Regressions reopen its status with a reason. Do not change the denominator/scope to inflate the percentage; record user-approved scope changes and differences from the previous baseline. Update Korean and English together.
