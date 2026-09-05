# Console and approval audit records

[한국어](../ko/control-action-audit.md) | English

## Contract and security boundary

This is the second IAM-001 audit batch. It retains the [feedback audit](feedback-audit.md) append-only guards, administrator-only reads, organization/repository isolation, and decision-time identity/role/request ID/correlation ID/ASGI IP policy. Existing requests, responses and approval gates are unchanged; audit responses gain a `details` object. Historical and feedback rows have `{}`; historical actions are not backfilled.

| Operation | `action` / `target_id` | `details` |
| --- | --- | --- |
| Console acquire/reacquire/release | `console.transferred` / work ID | `action`, `holder_type_before/after`, `holder_before/after`, `version_before/after`, post-operation UTC `expires_at` |
| Web PR/time-budget/console approve or reject; Slack PR approve | `approval.decided` / approval row ID | `kind`, `decision`, `budget_minutes_before/after`, `work_status_before/after`, `work_version_before/after`, `delivery_queued`, `delivery_bundle_sha256` |

PR approval that queues central delivery records the bundle SHA-256. Mock runs, rejections and other approval kinds record `delivery_queued=false` and a `null` hash. The hash identifies the approved bundle, not completed delivery or external repository contents. The existing `console` approval kind records a decision without transferring ownership; ownership changes use the separate console lease endpoint.

Successful mutations, activity events, approval rows, delivery reservations and audits share one transaction. Audit INSERT failure rolls back ownership/expiry/version, time budget/state/events/delivery reservations and prevents the delivery background task from starting. Insufficient permission, another organization, stale versions/states, another console owner or an unverified bundle produce no success audit. Auditing denied attempts remains future work. Arbitrary approval payloads, reasons and tokens are not copied into audits; existing approval/activity-log retention behavior is unchanged.

Synthetic request: `POST /api/work-items/{id}/approvals` with `{"kind":"budget","decision":"approve","payload":{"minutes":45}}` requires exhausted-budget state and approver permission. Success returns the existing WorkItem JSON; selected audit-read fields are:

```json
{
  "action": "approval.decided", "target_id": "7", "transport": "web",
  "required_role": "approver",
  "details": {
    "kind": "budget", "decision": "approve",
    "budget_minutes_before": 240, "budget_minutes_after": 285,
    "work_status_before": "budget_exhausted", "work_status_after": "implementing",
    "work_version_before": 6, "work_version_after": 7,
    "delivery_queued": false, "delivery_bundle_sha256": null
  }
}
```

## Time-budget input contract

For `kind=budget`, `decision=approve`, `payload.minutes` must be a **JSON integer** from 15 through 1440. Omitting it retains the 60-minute default. The range applies to one extension, not a new cumulative-budget cap. `null`, booleans, strings, arrays, objects, decimal notation (including `60.0`) and out-of-range integers return 422 with `{"detail":"invalid budget extension"}`. Budget, work state/version, approvals, events and audits remain unchanged, allowing retry with a valid integer.

Previously `"60"` was coerced into a number and `15.9` was truncated to 15; both are now rejected. Clients sending these values must confirm user intent and send a JSON integer (for example `{"minutes":60}`), not silently truncate input. Organization/repository permission, exhausted-budget state and quarantine checks remain unchanged. Budget **rejection** applies no extension and does not require `minutes` validation. PR/console approval payloads are outside this fix. No database, environment-variable or dependency changes are required.

## Migration and recovery

Back up first, then apply `20260906_0007` with `make migrate-api` and Alembic `check` before API rollout. The JSON column defaults to `{}` without rewriting existing audits. There are no new environment variables or dependencies. Online downgrade requires a locked, empty audit table. SQLite batch recreation removes existing triggers, so the migration reinstalls them. [Alembic batch behavior](https://alembic.sqlalchemy.org/en/latest/batch.html)

Any audit row or offline mode causes downgrade refusal, preserving data and revision. Do not bypass it with record deletion, disabled guards or manual stamping. Older APIs fail schema readiness, so binary rollback alone is not viable; recover with a forward fix retaining the current schema. This does not add external WORM, protection against database-administrator DDL, or automatic retention/export.

## Verification · 2026-09-06

Foundation `740d902`, console `4210b36`, approval/E2E `77b9a31`. Subsequent documentation/images do not alter runtime code.

- `make test`: API 288, Runner 6, Worker/Gateway, Web 40 and TypeScript passed. All 17 PostgreSQL tests skipped by the default run passed separately on the test database with `test_worker_postgres.py test_audit_postgres.py`. `make lint`, Web build and 12 Chromium E2E tests (about 1.1 minutes) passed. Existing Python/PostgreSQL and Web CI jobs automatically cover the added tests/migration; no extra job or matrix was added.
- Verified SQLite historical-row preservation, nonempty downgrade refusal, guards after empty downgrade, and PostgreSQL 17 upgrade/check/downgrade/re-upgrade/check. Audit-failure regression tests cover approval/ownership/delivery-reservation atomicity. Linked OIDC principals and signed Slack requests are integration fixtures, not live external-provider/Slack verification.
- Isolated API `:18520` and production-mode Web `:13520`: created work through Orca's Korean page; actual HTTP requests in its embedded browser verified console acquire 200 → stale release 409 → valid release 200. Clicked approval in native Chrome's English page and confirmed `committing`, removed approval controls and closed feedback. Verified three audits with before/after values, distinct request IDs, `no-store`, and unchanged records after duplicate approval 409.
- Built Web with `NEXT_PUBLIC_KELPIE_API_URL=http://127.0.0.1:18520` and matching runtime `KELPIE_API_URL`. The initial default-address build displayed a connection error with preserved input/retry guidance; rebuilding with the isolated address restored requests. Final KO/EN × 1440/390px views passed no-overflow, keyboard skip-link focus, post-approval read-only controls, and page/console/HTTP-error checks; captures were inspected.
- The worker is a scoped-credential HTTP protocol fixture, not a VM. Its lease expired during manual inspection; further worker transitions returned 401 and the local work was retained in `committing`. This does not claim actual SCM delivery, completion or resource-release verification after manual approval. The separate 12-test E2E suite includes the full mock completion journey. Images below are usage evidence, not UI implementation before/after comparisons.

![Korean post-approval activity and read-only controls](../assets/control-action-audit/ko-desktop.png)
![English narrow post-approval view](../assets/control-action-audit/en-mobile.png)

## Time-budget input regression verification · 2026-09-06

Fix `b76b8f4`; subsequent documentation/images do not alter runtime code.

- The new regression suite reproduced eight failures before the fix; all 23 tests passed afterward. It covers invalid types/ranges, retry, minimum/maximum extensions, omitted default, rejection and permission/state checks. Passed `make test-api` (311), 17 separate PostgreSQL tests, `make test-runner test-worker test-gateway test-web` (including Runner 6, Web 40 and TypeScript), `make lint`, Web build and the existing 12 Chromium E2E tests.
- Fresh isolated SQLite API `:18520` and production-mode Web `:13520`: a scoped HTTP worker fixture prepared exhausted-budget state. Actual requests in Orca's browser verified 422 for `null`, `"invalid"`, `"60"`, `15.9`, `60.0`, `[]` and `{}`, preserving budget 240/state/version and zero audit rows. Retrying 45 minutes returned 200, budget 285, `implementing`, version 6 and one audit whose request ID matched the response.
- Native Chrome inspection confirmed `Budget exhausted / 240 min` after rejection and `Implementing / 285 min` with live activity after valid approval. KO/EN × 1440/390px views passed updated-budget, input-label, skip-link focus, no-overflow and page/console/HTTP-error checks; captures were inspected. Expected HTTP 422 negative cases are separate from the error-free page checks.
- The worker fixture keeps authentication through standard lease renewal; it is not actual VM execution. This is API input validation, not a new time-budget control UI. Images are usage evidence, not UI design before/after comparisons.

![Korean page after a valid budget extension](../assets/budget-validation/ko-desktop.png)
![Updated budget on the English narrow view](../assets/budget-validation/en-mobile.png)

Cancellation/delivery audits, OIDC preview grants, audit retention/recovery, real KVM/WireGuard/noVNC input-ownership enforcement and concurrent-VM isolation remain MVP work. This change audits console leases; it does not implement a new console UI or VM input boundary.
