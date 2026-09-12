# Reconciliation of terminal Worker leases

[한국어](../ko/worker-lease-reconciliation.md) | English · [Credentials](worker-credentials.md) · [Progress](mvp-progress.md)

## Scope and trust boundary

The API lets an individually authenticated Worker inspect and reconcile an **already terminal work item's** lease after losing its per-run token on restart or losing a release response. This change alone does not implement automatic Worker restart recovery or VM cleanup.

- The Claim response's `lease_id` is the persisted `ResourceLease.id`, a public identifier, not a credential. Worker keeps `LeaseID` separate from `LeaseToken`. Never pass the Worker credential to a VM/Runner.
- The authenticated Worker ID, lease owner, work ID/assigned Worker and current work version must all match. Only `completed`, `failed` and `cancelled` work is accepted; its status/version is not changed.
- Lease state must be `active` or `released`. A Delivery Job must be absent or `completed`/`failed`. Running, quarantined and unknown states are refused. No approval or delivery authority is granted.
- `cleanup_confirmed: true` is a trusted Worker's **declaration** of physical cleanup. The API cannot independently prove remote disk removal or VM shutdown. The caller must first verify durable ownership, exact domain absence, absence of other VMs' disk references and file cleanup. Uncertainty means keeping reservations and not calling reconciliation.
- Lease expiry is not required: a restart can happen before expiry, and expiry does not prove VM shutdown. The endpoint neither renews expiry nor reissues a run token.
- Revoked/expired Worker credentials and quarantined Workers are denied. Even development shared tokens cannot use this route. No new environment variables or authentication relaxation are introduced.

Follow-up [Worker restart integration](worker-restart-recovery.md) persists the API-issued lease UUID as the schema-2 VM run UUID and has been verified with a real VM/API/PostgreSQL. Do not assume independent schema-1 run UUIDs satisfy this contract. Automatic adoption of legacy/ambiguous records, terminating nonterminal work, and recovery when the Claim response itself is lost remain separate work.

## API contract

`POST /api/workers/{worker_id}/claim` adds `lease_id`. Existing `work_item`, `lease_token` and `lease_expires_at` remain unchanged; an empty Claim is still `null`. The UUIDs and credential markers below are illustrative.

```http
GET /api/workers/33333333-3333-4333-8333-333333333333/leases/11111111-1111-4111-8111-111111111111
Authorization: Bearer <individual-worker-credential>
```

```json
{
  "lease_id": "11111111-1111-4111-8111-111111111111",
  "worker_id": "33333333-3333-4333-8333-333333333333",
  "work_item_id": "22222222-2222-4222-8222-222222222222",
  "state": "active",
  "work_status": "failed",
  "work_version": 3,
  "cpu": 2,
  "memory_mb": 4096,
  "disk_gb": 30
}
```

Inspection returns no token, hash, user requirements or repository contents. Compare its IDs/resources against durable records and complete physical cleanup, then use the response's current version when sending:

```http
POST /api/workers/33333333-3333-4333-8333-333333333333/leases/11111111-1111-4111-8111-111111111111/reconcile
Authorization: Bearer <individual-worker-credential>
Content-Type: application/json

{"work_item_id":"22222222-2222-4222-8222-222222222222","expected_version":3,"cleanup_confirmed":true}
```

Success is an empty `204`. Retry the same request after a lost response. `expected_version` must be an integer and `cleanup_confirmed` the JSON Boolean `true`; strings/numeric substitutes, missing or extra fields return `422`. Authentication failures return `401`, Worker-scope/individual-auth violations `403`, an unowned/missing lease `404`, and version/assignment/state conflicts `409`. Do not ignore a `409` and advertise local capacity as available.

## Atomicity and audit

Authentication holds Worker→Credential locks, followed by Lease→Work→Delivery Job. Existing release, Claim, Heartbeat, credential revocation and quarantine share the Worker lock. Only a committed transaction returns reservations; duplicates do not increment resources or repeat the release event.

The first reconciliation creates an append-only `lease.reconciled` audit containing Worker identity, work/lease IDs, correlation/request IDs, work status/version, returned resources and the cleanup declaration. This machine operation uses `transport=background`, with human roles, actor ID and source IP unset. If ordinary release already succeeded, reconciliation adds the declaration once with `lease_state_before=released`, without returning capacity again. An already audited lease that becomes `active` again is also refused. Resource updates and the audit commit/roll back together.

The DB schema/migration head (`20260909_0011`) is unchanged, with no new dependencies. Existing per-run-token release remains available. To roll back, first drain/stop Workers using this API, then revert the API code. Do not reactivate released leases or delete audit records. No DB downgrade is needed.

## Verification evidence — 2026-09-11

Implementation baseline: `f5d9da32c6500c413c4340d914997cda53b4e479` (including the Claim ID contract from `aff410b7781740736f6e8970b6306959c5a2c4c4`).

The [unaltered PostgreSQL verification log](../assets/worker-lease-reconciliation/postgres.log) has SHA-256 `86e9fc9a884662a0149ec47a23072427188a7cfefbc42e98271f1ad98a34d348`.

- [SQLite API specifications](../../apps/api/tests/test_worker_lease_reconciliation.py): 27 passed. First confirmed the unimplemented route failed with `404`.
- [Real PostgreSQL concurrency/HTTP checks](../../apps/api/tests/test_worker_lease_reconciliation_postgres.py): all 46 passed on local Mac Docker/PostgreSQL 17 (12.19 seconds). Used only a fresh temporary DB/role and UUID schemas, removed afterwards. Existing development data and audit guards were unchanged.
- Verified duplicate requests for one owned lease, other Worker progress, waiting requests denied after revoke/quarantine, ordinary release races/rollback, Claim accounting and unknown Delivery state denial. Independently reviewed missing declaration provenance and unknown-state acceptance were reproduced as real DB failures before fixing them.
- Ran Uvicorn on an ephemeral loopback TCP port: create→register→Claim→failed transition→lease expiry→inspect→invalid-auth/version denial→two concurrent reconciliations→one resource return/audit. The test uses an isolated PostgreSQL schema with lifespan/background jobs off. It is not production-startup, VM-shutdown or Browser/Console acceptance. No UI changed, so screen/keyboard checks are not applicable.
- GitHub Actions runs this regression in the existing PostgreSQL Worker gate. No new job, matrix or VM build. Full local verification and exact final-head CI results are recorded in the PR.
- On the final code, `make test` passed: API 1195 passed/152 skipped, Runner 45, Worker/Gateway, Web 125 plus type checking, and Lab 18; `make lint` also passed. The separate PostgreSQL CI command passed 16 existing+46 recovery tests, 62 total (15.34 seconds). Ordinary API skips reflect the unset dedicated PostgreSQL environment and a Linux-only check on Mac; all 46 new recovery tests ran separately against the real DB above. Remaining PostgreSQL gates are verified in final CI.

To reproduce, securely inject `KELPIE_TEST_POSTGRES_URL` for a dedicated, current-schema test DB, never production. Do not put raw URLs/tokens in command history or documentation.

```bash
make test-api
.venv/bin/python -m pytest -q apps/api/tests/test_worker_postgres.py apps/api/tests/test_worker_lease_reconciliation_postgres.py
make test
make lint
```

Without that URL, PostgreSQL checks skip and are not counted as passing. Follow-up actual Worker/VM terminal-lease recovery has [separate evidence](worker-restart-recovery.md). Nonterminal/lost-Claim recovery, per-work networking/time budgets, Preview/Console and two-work acceptance remain incomplete.
