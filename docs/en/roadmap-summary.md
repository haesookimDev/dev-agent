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

1. Replace `create_all` with Alembic and test upgrade/downgrade paths.
2. Add OIDC authentication, organization/repository authorization, and immutable approval audit records.
3. Move secrets to a production secret provider and rotate worker credentials independently.
4. Add OpenTelemetry traces, Prometheus metrics, structured correlation IDs, and retention jobs.
5. Build one pinned Ubuntu desktop golden image and complete a real libvirt run.
6. Establish WireGuard preview routing and enforce noVNC read-only/input ownership at the gateway boundary.
7. Run two browser-using jobs concurrently and prove that display, input, network, disk, and credentials remain isolated.

GitLab, advanced routing, and autonomous issue discovery should follow only after this security and execution baseline is proven.
