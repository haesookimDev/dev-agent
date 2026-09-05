# Safe delivery-failure diagnostics

[한국어](../ko/delivery-failure-safety.md) | English

Git output, arguments, file paths, and upstream exceptions can contain tokens or private repository content. The API delivery-failure handler no longer copies raw exceptions into `DeliveryJob.error`, `delivery.failed` events, or the `delivery.run` trace. Instead of pattern-based redaction, it records only code-defined stages and error categories.

## Event contract

The request and response for `POST /api/work-items/{id}/approvals` are unchanged. Successful approval means the decision was recorded, not that asynchronous delivery succeeded. A subsequent failure sets the work and delivery job to `failed`; the existing authorized SSE endpoint, `GET /api/work-items/{id}/events`, emits:

```json
{
  "event_type": "delivery.failed",
  "source": "delivery:github",
  "level": "error",
  "message": "GitHub delivery failed at apply (command_failed)",
  "payload": {"stage": "apply", "error_code": "command_failed"}
}
```

The example omits existing common fields such as IDs and timestamps. The database stores the same fixed message. API event messages and code identifiers are displayed identically in the Korean and English dashboards; they are not UI translation keys. The event structure remains compatible, with two additive payload fields. External consumers parsing the old free-form exception message must migrate to `payload.stage` and `payload.error_code`. Historical events may lack these fields.

| Error code | Meaning |
| --- | --- |
| `command_failed` | Git subprocess startup or execution failure |
| `timeout` | Delivery deadline or HTTP client timeout |
| `upstream_error` | Other HTTP client error |
| `filesystem_error` | Other OS/filesystem error |
| `internal_error` | Unclassified failure |

The stage is the operation in progress: `configuration`, `token`, `metadata`, `existing_pull_request`, `existing_branch`, `workspace`, `clone`, `checkout`, `apply`, `commit`, `push`, `pull_request`, or `finalize`. Temporary-directory cleanup errors are included in the last Git stage. Traces record `kelpie.delivery.stage`, `kelpie.delivery.error_code`, a safe error status, and a replacement exception, without the original chained causes or stack trace. Work/correlation IDs and existing attempt/outcome metrics are preserved.

## Operations and compatibility

- Start with the work/correlation ID, stage, and error code. For example, investigate an `apply` failure by checking the approved patch against its base branch in an access-controlled environment. Never paste original tokens, command output, or private patches into issues or shared logs.
- Successful `run_command` output is unchanged. Failures retain only an exit code or a fixed startup-failure message, without arguments, stdout, or stderr. Cancellation still terminates the owned process group.
- Approval gates, Worker quarantine, write deadlines, and duplicate-PR prevention are unchanged. A quarantined job is not overwritten with an ordinary failure.
- No new environment variables, dependencies, or schema migration are required. Deploy the API code to protect subsequent failure records. Historical database records, events, and external traces are not rewritten or deleted automatically. Investigate access and revoke credentials under a separate incident-response process if historical exposure is suspected.
- Rolling back to the old code restores raw-error exposure. If problems occur, stop delivery processing and deploy a corrected version; do not resume publicly exposed production delivery on the vulnerable version.

## Verification evidence

The implementation at `6ea6a7f` was verified as follows.

- Eight regression tests failed before the fix. Afterward, real subprocess output, paths, chained causes, HTTP failures, and timeouts were checked for absence from database errors, events, and in-memory traces.
- Real Uvicorn, migrated temporary SQLite, a production Next.js build, and an OTLP HTTP collector were started. A synthetic GitHub HTTP server and local Git repository were used, not a real GitHub account or VM.
- A scoped Worker protocol fixture advanced the work to approval and uploaded a patch. No delivery token was issued before approval. The approval button was clicked directly in Orca's browser; real `git clone` and `checkout` were followed by a failing `git apply`. There was one approval audit record, one delivery-failure event, and no PR creation.
- The original Git error was first confirmed to contain a synthetic private path. After delivery handling, that value and the test delivery token were absent from the database error, events, transmitted OTLP data, and API logs. The temporary Git workspace was cleaned up.
- Korean and English screens directly displayed the safe error and failed status. At a 1035px browser width there was no horizontal overflow or console error; approval and state requests returned 200. The desktop tool had no visible window, so native interaction could not be performed. No native/UI code changed.
- Rerun `make test-api` and `make lint`. `test_delivery_disclosure.py` also contains a real Git clone/patch-failure regression without external networking. Set `KELPIE_TEST_POSTGRES_URL` to an isolated test database for PostgreSQL-specific tests.
- At `da49e80`, including the real Git regression, all 342 API tests passed with PostgreSQL enabled (26 seconds), as did the full `make lint`. The production Web build also passed.

Owned services, browser tab, temporary database, keys, tokens, and repositories were cleaned up afterward. This change protects one boundary: delivery-failure evidence. It does not guarantee secret-free logs, artifacts, crash dumps, cloud-init, or database-failure paths generally, nor complete SEC-001. See [secret management](secret-management.md) and the [MVP roadmap](roadmap-summary.md) for remaining scope.
