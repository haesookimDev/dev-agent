# Database readiness failure/recovery verification

[한국어](../ko/readiness-verification.md) | English

## Contract and scope

`inspect_schema` applies one two-second deadline to database pool checkout, pre-ping, connection establishment, and Alembic revision lookup. Startup inspection and `GET /readyz` share this implementation. It uses Python asynchronous cancellation: connection cleanup and scheduling overhead mean the entire response is not guaranteed to take at most exactly 2.000 seconds.

| Condition | HTTP response | Body |
| --- | --- | --- |
| Current schema | 200 | `{"status":"ok","database_schema":"current"}` |
| Unresponsive database or inspection timeout | 503 | `{"status":"not_ready","database_schema":"unreachable"}` |
| Revision mismatch | 503 | Existing `outdated` or `unversioned` state |
| `GET /healthz` | 200 | `{"status":"ok"}` — process liveness only |

API fields, states, authorization, and approval policy are unchanged. `SCHEMA_READINESS_SECONDS` is an internal code constant; there are no new environment variables, dependencies, or schema migrations. This does not impose a global timeout on ordinary application queries, entire migrations, or explicit development bootstrap. Caller-driven `CancelledError` is propagated rather than converted to `unreachable`.

An inspection taking longer than two seconds is not ready even if the revision is correct. Investigate pool exhaustion, networking, database load, and schema locks. `unreachable` includes other connection failures and is not a timeout-specific code. Responses omit database addresses, passwords, and original errors.

After connection recovery, the next `/readyz` request inspects again. If the schema was unready at startup, [startup delivery recovery](delivery-recovery.md) retries in the background. A later 200 from `/readyz` alone is not evidence that the delivery queue completed. Continuous delivery-worker health remains separate OBS-001/OPS-001 work. Database readiness also does not establish object-store, SCM, or actual VM health.

## Verification results

Verified implementation commit: `e951870`.

- Three regressions failed before the fix and passed afterward: connection waiting, schema lookup, and a real unresponsive PostgreSQL peer. Coverage also includes two caller-cancellation cases and a real Uvicorn process.
- `KELPIE_TEST_POSTGRES_URL=<isolated test database> make test-api`: 348 tests passed with PostgreSQL enabled (about 31 seconds). `make lint` passed.
- The real API-process test uses an unresponsive PostgreSQL handshake server. It verifies bounded startup, `/healthz` 200 while `/readyz` waits and returns 503 after about two seconds, and absence of the test password from logs. Each run cleans up its own ports and processes.
- Direct runtime verification used a separate PostgreSQL 17 database, migrated schema, controllable local TCP proxy, and real Uvicorn. No production database or user data was accessed.

| Directly exercised condition | Observed result |
| --- | --- |
| Startup with unresponsive database | `/healthz` 200 (0.001s), `/readyz` 503 (2.005s) |
| Restore connection without restarting API | `/readyz` 200, `current` (0.011s) |
| Lock `alembic_version` in another transaction | `/readyz` 503 (2.034s), `/healthz` 200 |
| Roll back the lock | Next `/readyz` 200 |
| Hold the only connection in a size-one pool | Inspection `unreachable` (2.005s) |
| Return pool connection | Next inspection `current` |
| Stall connections again | Real browser fetch 503 (2.008s), zero remaining proxy connections |

In Orca's browser, `/readyz` was opened and its reload/JSON controls were directly operated to observe `unreachable → current → unreachable`. Browser screenshots were inspected, and the desktop window was also observed through Computer-use. No native-input or Web UI code changed, so this is not new acceptance evidence for those features. Endpoint identifiers are language-neutral; this guide is synchronized in Korean and English.

Owned API, proxy, tab, logs, and disposable database are cleaned up after verification. Rolling back means deploying the previous API code, which also removes the stalled-inspection deadline. This change alone does not complete external dependency health, alerts, retention, or recovery in the [MVP](roadmap-summary.md).
