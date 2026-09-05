# Worker control-plane quarantine

[한국어](../ko/worker-quarantine.md) | English · [Credential operations](worker-credentials.md)

This command blocks **control API access and subsequent delivery** for a suspected compromised Worker. It does not terminate VMs, isolate host networking, or close existing Preview/WebSocket connections. It is not complete incident containment or completion of all SEC-001 requirements.

## Execute and verify

Run with database administrative authority on the control host. Ordinary organization-admin HTTP permissions do not gain global Worker quarantine authority. The updated API and schema `20260905_0005` are required; there is no additional migration or environment variable.

```bash
.venv/bin/python -m app.worker_admin list
.venv/bin/python -m app.worker_admin quarantine \
  --worker-id <target-Worker-ID> --reason 'Suspected compromise response'
```

Resolve the target first. Never include tokens or personal information in the reason. Output contains metadata, not plaintext credentials:

```json
{"worker_id":"<Worker-ID>","already_quarantined":false,"revoked_credentials":2,"invalidated_leases":1,"affected_work_ids":["<Work-ID>"],"physical_cleanup_required":true}
```

One database transaction performs these actions:

- Mark the Worker `offline`, revoke every unrevoked credential, and quarantine all active leases. Shared development-token access is not restored.
- Transition active work to `cancelled`, or `committing`/`pr_created` work to `failed`. Preserve existing `completed`, `failed`, and `cancelled` outcomes.
- Mark pending/retry/running delivery jobs `quarantined` and expire Preview/Console leases. Management history records UID/reason; per-work events contain only a generic quarantine notice and Worker ID.
- **Keep resource reservations held.** CPU, memory, and disk must not appear reusable without confirming that the VM has stopped.

Repeated execution succeeds without duplicate quarantine events. Issuance/rotation for the quarantined Worker is rejected. Unlike routine `revoke`, work leases are also invalidated.

## API and delivery boundaries

| Request | Result after quarantine commits |
| --- | --- |
| Worker registration/heartbeat/claim and work-lease requests | `401` — existing credentials cannot access them |
| Work feedback/approval/console acquire or release, linked Slack commands | `409`, `{"detail":"work's worker is quarantined"}` |
| New Gateway Preview/Console resolution | `410` — no target address returned |
| Authorized work/event reads | Remain available for incident review |
| Other Workers' execution/delivery | Continues |

PostgreSQL row locks follow Worker → Lease/Work → Delivery Job/Preview/Console ordering. Even after approval, GitHub token issuance, Git push, and PR creation recheck state immediately before each external request and hold the Worker lock throughout that request. Quarantine waits for already-started writes; each external write has a 45-second deadline including lock acquisition. Cancelling Git terminates its owned process group. No new delivery write starts after quarantine commits, and late failure handling/server restart does not overwrite quarantine state.

A request already accepted by SCM may succeed remotely even after a local timeout. Previously started pushes/PRs and already-issued external tokens are not automatically deleted/revoked; inspect SCM audit history and take separate action as appropriate. Existing Preview connections are not disconnected by this API check. SQLite is for demos/functional tests and does not provide PostgreSQL's concurrency guarantees.

## Physical response and recovery

Use approved operational procedures to isolate the target host's network/WireGuard access, terminate its VMs and open Preview connections, and revoke potentially exposed credentials. Preserve logs and artifacts according to incident-evidence policy. The CLI does not delete host files or VMs.

There is no unquarantine or forced resource-release command. After remediation and verified physical cleanup, provision a clean host under a new Worker identity, review artifacts, and retry as a new work item. Do not reset database flags or fall back to shared development authentication. Older versions lack delivery fencing: when rolling back, stop external traffic, Workers, and delivery processes, preserve quarantine metadata, and roll forward to a corrected version.

## Verification

- `make test-api`: all work states, idempotency, CLI, nine lease endpoints, user/Slack/Preview denial, delivery-stage quarantine, successful delivery, late failures, deadlines, and cancellation of real child processes.
- Set `KELPIE_TEST_POSTGRES_URL` to an isolated PostgreSQL database with the latest schema and run `pytest -q apps/api/tests/test_worker_postgres.py`: both lease/Preview/delivery quarantine races, independent Worker progress, and lock release on publication timeout. The existing PostgreSQL CI step runs these tests too.
- Actual-use verification uses a separate API, two scoped-authenticated Mock Workers, and the dashboard to confirm target-work cancellation and independent Worker completion. Real Linux/KVM, WireGuard, and noVNC isolation remain separate P1 acceptance requirements.
