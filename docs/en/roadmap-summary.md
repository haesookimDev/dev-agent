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
2. **Partially complete:** OIDC authentication and organization/repository authorization are implemented. Immutable detailed approval audit records remain.
3. **Partially complete:** Environment/file secret providers, per-worker issuance/overlapping rotation/revocation/reloading, and [control-plane quarantine](worker-quarantine.md) are implemented. Credentials, active leases, user mutations, new Preview resolution, and subsequent delivery are fenced together. Actual host/VM/network/existing-connection containment and comprehensive secret-leak checks remain. [Credential operations](worker-credentials.md)
4. **Partially completed:** OpenTelemetry traces, Prometheus metrics, and structured correlation IDs are implemented. External dependency readiness, alerts, and retention jobs remain.
5. Build one pinned Ubuntu desktop golden image and complete a real libvirt run.
6. Establish WireGuard preview routing and enforce noVNC read-only/input ownership at the gateway boundary.
7. Run two browser-using jobs concurrently and prove that display, input, network, disk, and credentials remain isolated.

GitLab, advanced routing, and autonomous issue discovery should follow only after this security and execution baseline is proven.
