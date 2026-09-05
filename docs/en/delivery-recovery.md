# Startup delivery recovery

[한국어](../ko/delivery-recovery.md) | English

## Behavior and operational boundaries

When the API starts with an unavailable database or an unready schema, delivery recovery waits in the background. It retries five seconds after a readiness check or recovery attempt finishes, then exits after successfully processing one scan of a ready database. API startup does not wait for delivery completion.

- Scan `pending`, `retry`, and `running` jobs. Register active work before the first asynchronous wait so an approval request and startup recovery in the same process cannot execute the same job concurrently.
- Reset only `running` jobs not active in this process to `retry`, as before. Preserve Worker → Work → DeliveryJob lock ordering and existing approval, verified-bundle, and quarantine checks. Do not automatically reapprove or execute completed, failed, or quarantined jobs.
- Apply separate two-second cooperative deadlines to the scan and recovery state changes. Readiness has its own two-second deadline. This does not introduce a global timeout for entire SCM deliveries or ordinary application queries.
- Recovery awaits its delivery children and retrieves exceptions. Cancellation cancels and joins those children. If recovery cannot finish, for example because database processing fails, log the fixed warning `delivery recovery failed; retrying` and retry. Do not include original connection strings or exceptions in this warning.

Duplicate-execution protection is **local to one API process**. It does not establish ownership across multiple Uvicorn workers, API replicas, or overlapping old/new APIs using the same database. Retain the single-process Compose/Dockerfile configuration. During replacement, settle and stop the old API and its deliveries before starting the new one. Distributed leases/fencing are a prerequisite for multi-process operation.

This is startup recovery, not a continuous queue worker. Periodic discovery of newly orphaned jobs after the first successful recovery, automatic SCM-failure retries, throughput/latency SLOs, and alerts are separate work. [Recovery-state metrics](delivery-recovery-metrics.md) observe the startup procedure, not individual delivery success. A 200 from `/readyz` verifies only the schema, not a completed delivery queue or healthy external SCM. Inspect `delivery_jobs.state/attempts`, work status/events, and existing delivery metrics together.

API responses, schema, environment variables, and dependencies are unchanged. `DELIVERY_RECOVERY_RETRY_SECONDS=5` and `DELIVERY_RECOVERY_DB_SECONDS=2` are internal code constants. Rollback can restore the previous API, but reintroduces missed delivery resumption after unready startup and untracked recovery tasks. No migration or downgrade is required.

## Verification

The record below covers implementation `1e3db7a` and real-process test `c166d09`. See the [separate record](delivery-recovery-metrics.md) for subsequent metric verification.

- Eight regression tests failed before the fix. They cover delayed recovery, process-local duplicate prevention, successful delivery, retry/safe warnings, and cancellation of children during shutdown.
- `KELPIE_TEST_POSTGRES_URL=<isolated PostgreSQL> make test-api`: 357 passed in approximately 37 seconds. `make lint` passed. The 35 related delivery tests also passed separately.
- A real Uvicorn/SQLite test checks zero attempts during a schema mismatch, then one attempt each for `pending/retry/running` jobs after schema recovery. It deliberately uses the failure path with no external GitHub configuration; the quarantined job retains its state and zero attempts. It also checks graceful shutdown and absence of unretrieved task exceptions. It runs in the default API CI without extra services.
- Direct acceptance used a dedicated PostgreSQL 17 database, a fault-injecting TCP proxy, real Uvicorn, a production Web build, and a local GitHub HTTP fixture. The worker is an API protocol fixture, not an actual VM. No external GitHub writes occurred. The existing-branch path was used, so this is not evidence of a real successful Git push.

| Directly exercised journey | Observed result |
| --- | --- |
| Upload verified bundle, then approve in the browser | Zero token/PR requests before approval; `committing` after approval |
| Stop API during delivery, restart with unresponsive DB | Job remains `running` with one attempt; browser `/healthz` 200 and `/readyz` 503 in 2.008 seconds |
| Restore only database connectivity | `completed` without another API restart or approval; two total attempts, one PR creation request, one approval audit |
| Same browser event stream reconnects | Korean `완료`, English `Completed`, 100%, and PR link visible |
| Final state | `/readyz` 200; synthetic DB password and SCM token absent from API/Web logs |

Approval and language switching were directly operated in the Orca browser and screenshots inspected. No console messages were captured; the intentional outage interrupted streams and returned 503, followed by successful reads/streams. Computer-use observed the desktop window, but native input was not tested. Web UI and native-input code are unchanged in this patch.

After verification, clean up owned API/Web/fixture/proxy processes, browser tab, keys, tokens, temporary files, and the disposable database. Leave user data and unrelated processes untouched. [MVP](roadmap-summary.md) external-dependency health, alerts, retention, and real KVM isolation still require separate work and verification.
