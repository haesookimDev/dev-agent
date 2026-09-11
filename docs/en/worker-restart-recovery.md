# Terminal-lease recovery after Worker restart

[한국어](../ko/worker-restart-recovery.md) | English · [Lifecycle](worker-lifecycle.md) · [API contract](worker-lease-reconciliation.md)

## Implementation and safety boundary

Verified code/test baseline: `b9fdd484f4d7777345e89694c0c2a00b42f87816`. Check [progress](mvp-progress.md) and the PR separately for `main` merge status and final CI.

1. Persist the Claim's public lease UUID as schema-2 `run_id` before VM commands. Never recreate an existing lease directory or persist its run token.
2. Lock the WorkRoot, validate durable ownership/domains/foreign disk references and clean only the remaining owned VM and exact files.
3. Register and inspect with the individual Worker credential, matching Worker/Lease/Work IDs and reserved resources. Only terminal work is accepted.
4. Recheck physical absence after the network read. Persist `released` only after the reconciliation API returns exactly `204`.
5. Read fresh registration data for the same Worker, requiring zero active runs and full configured capacity before starting heartbeats/claims.

Registration/inspection require exact `200`, bounded JSON, mandatory fields/types and matching identity. Missing active-run counts do not become zero. Duplicate keys, nulls, case aliases, trailing JSON, redirects and incorrect acknowledgement statuses are rejected. Each request reloads the credential file; Worker authentication is never sent into the VM or a run-token header.

Lost responses leave `cleaned` intact for the next startup to inspect server state. API idempotence and durable local `released` records prevent duplicate capacity/audits/events. The API still trusts the Worker's cleanup declaration rather than independently inspecting a remote VM.

## Actual verification — Linux inside Mac, 2026-09-11

[Unaltered final log](../assets/worker-lifecycle/actual-restart-postgres.log), SHA-256 `c550e90009cc1f668a2f21e4688bc49e56c1e2661cc40e00d2e23e2332e8575f`.

- Go 1.24.1, Linux/ARM64, CGO disabled. The real Worker binary embeds the above commit and `vcs.modified=false`, SHA-256 `99f8b64e27db8cb1ad3a12280ca0665fce8912f222edf3994242398ca3b2f544`. Test binary: `87f8426f5c2928e015319a9af79320dc7dc464f1ac3ced140db173476e28b16c`, byte-identical before/after the test commit.
- Used the dedicated Lima Ubuntu 24.04 host's unprivileged Worker/libvirt account and actual KVM. Created only a small blank disk, empty cloud-init and owned NVRAM, without NIC/display. No existing Golden Image or user disk was used.
- Mac Uvicorn listened on an ephemeral loopback TCP port, with a fresh PostgreSQL 17 database/role/schema. SSH forwarded only the Linux host's loopback port, without forwarding the SSH agent or mounting Mac Home. Lifespan/background jobs were disabled in this fixture; this is not production-startup verification.
- Real API Claim→ownership record→running VM→failed work transition, followed by a new Worker executable without the run token. After cleanup, reconciliation and full-capacity confirmation, observed its ready log and shut it down cleanly. A second new process started and stopped against the same journal.
- Go test **passed in 22.15 seconds**; PostgreSQL-inclusive check **1 passed in 22.88 seconds**. Final DB: CPU 4, memory 8192 MiB, disk 60 GiB, zero active runs, `released`, one release event/one reconciliation audit, work `failed`/version 3.
- Final domain inventory and forwarded port were empty; temporary credential files, database/role and SSH/API processes were cleaned. Only the journal remains at `/var/tmp/kelpie-lifecycle-2234943308/e0fec201-cf78-43ce-bc8c-bfe5092bd9e5`. Removed blank disks/seeds/NVRAM are reproducible fixtures; existing images/data were preserved.

The fixture prepares a real running VM's durable records and a **new Worker process** recovers them. This does not inject SIGKILL into a complete old Executor process, prove Golden Image OS/Runner execution, or replace GUI/browser/console/concurrent-two-work acceptance. No UI changed, so screen/keyboard checks do not apply.

## Reproduction and CI

Run the [Go fixture](../../apps/worker/internal/daemon/libvirt_api_recovery_integration_test.go) with the [API/PostgreSQL controller](../../apps/api/tests/test_worker_vm_recovery_postgres.py). Copy this commit's Linux ARM64 Worker and `libvirt_integration` test binaries to an approved empty Linux KVM lab, then prepare this secret-free local configuration. SSH must resolve to the loopback Lab and the `kelpie` user.

```json
{
  "acknowledgement": "disposable-host-and-isolated-api-only",
  "ssh_config": "/absolute/path/to/lima/kelpie-kvm/ssh.config",
  "ssh_host": "lima-kelpie-kvm",
  "test_binary": "/home/kelpie/worker-recovery-02.test",
  "worker_binary": "/home/kelpie/worker-recovery-02",
  "forward_port": 58867
}
```

Securely inject `KELPIE_TEST_POSTGRES_URL` for a dedicated current-schema test database. The test issues a credential and passes it only through stdin/a `0600` file, never command arguments or documentation.

```bash
KELPIE_TEST_LIBVIRT_RECOVERY_CONFIG=/absolute/path/recovery-lab.json .venv/bin/python -m pytest -q -s apps/api/tests/test_worker_vm_recovery_postgres.py
make test
make lint
```

Ordinary API tests skip this opt-in check; CI success is not substitute evidence. No extra VM build/job/matrix is added, and existing Go CI runs ordinary recovery regressions. Final `make test`: API 1195 passed/153 skipped, Runner 45, Web 125/type checking, Worker/Gateway and Lab 18 passed. `make lint` and focused Worker race tests also passed. The one new opt-in check passed separately in the real environment above; final CI verifies existing PostgreSQL/Linux gates.

## Rollout, rollback and remaining conditions

Deploy the API's Claim `lease_id`, inspection and reconciliation routes first. Drain/stop and check ownership/credentials before rollout; current recovery startup requires an online Worker. No API schema/DB migration, production environment variable or dependency changes. The configuration and acknowledgements above are test-only.

Schema 1 and old `<WorkID>` directories cannot be automatically adopted as leases. Do not edit/delete records to bypass recovery guards. Stop the Worker with verified cleanup and preserve journals/audits before rollback; do not immediately start a schema-2-unaware version on the same WorkRoot.

Nonterminal recovery policy, lost Claim responses, complete orphan handling, full Executor/Golden Image/Runner, per-work networking, time budgets, Preview/Console and concurrent two-work acceptance remain. Fixed release-stage completion stays **1/7 = 14.3%**.
