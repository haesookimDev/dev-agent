# Cancel unassigned queued work

English | [한국어](../ko/work-cancellation.md)

## Scope and usage

Administrators can cancel unwanted or duplicate work before Worker execution. On the work detail page, select **Cancel queued work…**, review the title and impact, then choose **Confirm cancellation**. Initial focus is on **Go back**; closing with Esc before submission does not change the work.

- The server requires all three conditions: `queued`, `assigned_worker_id=null`, and no Resource Lease record. Expired and released leases also count as execution history and are rejected.
- Cancellation sets `cancelled` and increments the version once. Claim no longer selects it. Work, events, and audit history are retained, not deleted. Undo and automatic recreation are not provided.
- Running, approval-waiting, delivering, failed, and completed work cannot be cancelled through this feature. It does not terminate VMs, fence networks, force-release leases, quarantine Workers, or provide Slack cancellation.
- UI visibility uses queued state and lack of assignment. The server rechecks permissions and lease history. Seeing a button does not grant administrator access.
- Submission prevents duplicate actions. The browser limits the cancellation request to ten seconds and the follow-up status refresh to five seconds. A connection failure does not mean the server did nothing. Check the latest status and do not resubmit cancelled work.
- Success updates status and feedback controls and moves focus to the status panel. Unsent feedback remains read-only and copyable until navigation; drafts are not saved to the server.

## API and authorization

```http
POST /api/work-items/00000000-0000-4000-8000-000000000042/cancel
Content-Type: application/json

{"expected_version": 1}
```

Use an authenticated Web session and same-origin request. The effective role, including the organization baseline or a grant for this repository, must be `administrator`. Even administrator access does not cross organization boundaries. Actual session values are omitted from examples.

Success returns the entire existing `WorkItemView`; key fields are shown below.

```json
{"id":"00000000-0000-4000-8000-000000000042","status":"cancelled","version":2,"assigned_worker_id":null}
```

| Response | Meaning |
| --- | --- |
| 200 | Cancellation, state event, and audit record committed in one transaction |
| 401 / 403 | Authentication failure, insufficient administrator role, revoked membership, or rejected Origin |
| 404 | Missing work, another organization, or an unregistered repository |
| 409 | Version conflict, already cancelled, or ineligible state/assignment/lease history |
| 422 | Missing/invalid positive integer `expected_version`; no string or Boolean coercion |

Repeated requests return 409 instead of processing another success. Use the version from the latest read; a state change after confirmation requires another review. Existing response shapes, states, Worker contracts, and environment variables are unchanged. No database migration is needed. Deploy the API before Web.

## Auditing and concurrency

Success appends `work.cancelled` to existing append-only `audit_records`. It captures Actor ID, subject, identity provider, organization/repository/effective/required roles, request/correlation IDs, ASGI peer IP, and work/repository/organization identities. User-supplied forwarded headers are not trusted as source IP. `details` contains only `scope=unassigned_queue` and before/after states and versions; no free-form reason, token, or raw request is copied.

Read it through existing administrator-only `GET /api/work-items/{id}/audit-log`, retaining `Cache-Control: no-store`. An audit insert failure rolls back cancellation, version, update time, and state event together. Rejected requests create no success audit. Existing [audit storage protections](feedback-audit.md) remain in place.

Production concurrency uses PostgreSQL work-item row locks. Cancellation locks the same row as Claim without adding a reversed Worker-lock dependency. A cancellation waiting behind a committed Claim reads the new assignment and fails. Claim uses `SKIP LOCKED` to select other work while cancellation holds its target. After cancellation rollback, a subsequent Claim can acquire the original work. Standalone SQLite execution is not a substitute for PostgreSQL concurrency verification.

## Verification — 2026-09-06

Implementation commits: API `0f2fd57`, PostgreSQL races `d0bb7fa`, CI `6eb0aae`, Web `2e38022`, focus regression fix `1b7bd5e`. Subsequent documentation/images do not change behavior; exact final-head revalidation is recorded in the PR.

- `KELPIE_TEST_POSTGRES_URL=<dedicated test DB> make test`: API 405 (including 34 new cancellation cases and four real PostgreSQL races), Runner six, Web 52 plus types, Worker/Gateway passed. Do not use production credentials for the test URL.
- `make lint`, `npm run build --prefix apps/web`: passed.
- `npm run test:e2e --prefix apps/web`: 18 passed against real API, migrated temporary SQLite, single-slot Mock Worker, and Chromium; about 90 seconds locally. Six new cancellation journeys cover both languages, narrow layout, Esc/focus, duplicate submission, 403 presentation, actual 409, lost success responses, and SSE changes during confirmation. Web 403 presentation uses an explicit response fixture; actual OIDC authorization denial is tested by the API suite.
- The full E2E suite caught an unnecessary error rendered in the closed confirmation dialog. Errors now render only while a confirmation is active; the unchanged existing tests passed on the full rerun.
- Actual production-browser use reproduced focus falling to `BODY` after cancellation. Focus now moves to the persistent status panel immediately after closing, independently of deferred `close` events and React state timing. A regression assertion observes focus before the event: it failed with `false` before the fix and passed in both languages afterward. Web 52, types, lint, and all 18 E2E tests passed again.
- Separate real API `127.0.0.1:18440` and production Web `127.0.0.1:13440` were launched. Direct Orca browser use verified creation, confirmation, Esc/back, keyboard submission, status, one audit, version `1→2`, no assignment, and closed feedback. Korean desktop and 390px English views verified long titles, horizontal overflow, and initial focus. Mobile uses desktop Chromium device emulation, not a physical iPhone.
- Computer Use visually verified the actual dashboard in Orca's desktop window. The environment had no native focus, so OS keyboard/mouse input was not verified. This PR changes no native input, Console, or VM code.
- The final Korean cancellation returned HTTP 200 with no hidden error elements. Both Korean/English verification tabs had no console messages. The actual default cancellation button's text/background contrast was approximately 6.55:1.
- Direct runtime used isolated development authentication, with no Worker, SCM, or external notifications running. These results do not prove enterprise IdP integration, production deployment, or real KVM cancellation.

Required `Python` CI runs the four PostgreSQL races in a dedicated step, taking about 1.2 seconds locally. Existing required check names, eight-minute limits, read-only token permissions, and full application coverage remain unchanged.

## Screens

Before/after screenshots contain only test data. Work IDs/timestamps differ; generated screenshots were not edited.

![Before](../assets/work-cancellation/before-ko.png)

![Cancellation controls](../assets/work-cancellation/after-ko.png)

[Korean confirmation](../assets/work-cancellation/confirm-ko.png) · [390px English confirmation](../assets/work-cancellation/confirm-mobile-en.png)

[Status focus and preserved draft after cancellation](../assets/work-cancellation/cancelled-ko.png)

## Remaining scope and rollback

This batch does not complete IAM/OPS. Active-run administrator cancellation still needs verified physical VM termination/cleanup and exactly-once resource release. Complete delivery auditing, retention jobs, backup/restore, direct OIDC Preview validation, and real KVM/network/two-work isolation also remain.

Roll back Web/API code to the previous version while retaining cancelled work and append-only audits. Do not revive work through manual SQL or delete its audit. Previous versions already understand `cancelled`. An authorized user can create a new work item if execution is needed again.
