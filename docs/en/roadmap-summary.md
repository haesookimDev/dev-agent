# Next development summary

[한국어](../ko/roadmap-summary.md) | English · [Detailed plan](roadmap-detailed.md)

## Goal

Turn the current deployable vertical slice into a production-grade, multi-worker autonomous development platform that can safely discover, implement, verify, and deliver changes without giving repository or host authority to task agents.

## Recommended order

| Priority | Milestone | Outcome | Release gate |
| --- | --- | --- | --- |
| P0 | Production foundation | Migrations, OIDC/RBAC, secrets, audit, metrics, retention | No development authentication or implicit schema creation in production |
| P1 | Real KVM execution | Reproducible golden images, VM networking, WireGuard previews, enforced console ownership | A real repository completes two isolated concurrent runs on one host |
| P2 | Verification and evidence | Policy-driven tests, browser/computer-use evidence, artifact storage, evaluator gates | PR approval always includes reproducible machine and UI evidence |
| P3 | Provider and messaging adapters | GitLab parity, GitHub Checks, Slack/messenger result delivery and feedback | The same work lifecycle runs through either SCM provider |
| P4 | Scheduling and orchestration | Resource/time prediction, fair routing, sub-agent DAGs, checkpoint recovery | Multi-worker load and failure tests meet defined scheduling SLOs |
| P5 | Autonomous discovery | Scheduled security, dependency, bug, and quality discovery with deduplication | Agents may propose issues but can never label, approve, or deliver their own proposal |
| P6 | Scale and operations | HA control plane, quotas, cost accounting, backup/restore, upgrade strategy | Disaster recovery and tenant-isolation tests pass |

## Immediate next release

The next release should contain only P0 and the smallest P1 end-to-end path:

1. **Completed:** Replace `create_all` with Alembic and test upgrade/downgrade paths.
2. **Partially complete:** OIDC authentication, organization/repository authorization and append-only [feedback](feedback-audit.md), [console/approval](control-action-audit.md), [unassigned queued cancellation](work-cancellation.md), and [approval-linked delivery](delivery-audit.md) audits are implemented. Active-run administrator cancellation and OIDC preview grants remain.
3. **Partially complete:** Environment/file secret providers, per-worker issuance/overlapping rotation/revocation/reloading, and [control-plane quarantine](worker-quarantine.md) are implemented. Credentials, active leases, user mutations, new Preview resolution, and subsequent delivery are fenced together. Actual host/VM/network/existing-connection containment and comprehensive secret-leak checks remain. [Credential operations](worker-credentials.md)
4. **Partially completed:** OpenTelemetry traces, Prometheus metrics, structured correlation IDs, [bounded DB readiness](readiness-verification.md), [startup delivery resumption after DB recovery](delivery-recovery.md), [recovery-state metrics](delivery-recovery-metrics.md), [baseline alerts](monitoring-alerts.md), and [continuous Worker/lease/queued-work observations](runtime-monitoring.md) are implemented. External dependency readiness, stalled running-work/DeliveryJob coverage, an integrated operations dashboard, and non-ordinary-file retention policies/scheduled cleanup remain.
5. Build one pinned Ubuntu desktop golden image and complete a real libvirt run.
6. Establish WireGuard preview routing and enforce noVNC read-only/input ownership at the gateway boundary.
7. Run two browser-using jobs concurrently and prove that display, input, network, disk, and credentials remain isolated.

P0 OPS-001 also includes [PostgreSQL backup/new-database restore verification for data, permissions, and audits](postgres-restore.md) and [active-work-safe ordinary-file retention CLI, expiration UI and backup V2](artifact-retention.md). Object-store recovery, other data retention policies/scheduled janitors, and actual operational recovery verification remain.

GitLab, advanced routing, and autonomous issue discovery should follow only after this security and execution baseline is proven.
