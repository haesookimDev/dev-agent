# Human authorization boundary for Worker transitions

[한국어](../ko/worker-transition-authority.md) | English

## Contract

A work lease authorizes execution reporting, not human approval. `POST /api/runs/{id}/transition` checks the validated lease's Worker ID against the current assignment and applies an explicit allowlist narrower than the common state graph. Approval claims in payload/message and actor strings do not establish authority. Adding a platform graph edge does not implicitly grant Worker access.

| Transition category | Authority boundary |
| --- | --- |
| Analysis/implementation/verification progress and requests to wait for input/feedback/approval/budget | Only explicit Worker edges are allowed |
| Resume implementation from input/feedback/approval waits | Requires authorized user feedback or the applicable approval decision |
| `budget_exhausted → implementing` | Requires user budget approval; a Worker cannot assert an extension |
| `awaiting_approval → committing` | Requires user PR approval |
| `committing → pr_created → completed` | Only an approved Mock under the additional checks below; central delivery belongs to the control plane |
| Platform transitions such as requeueing failed work or assignment | A Worker lease alone is insufficient |

Failure/cancellation reports explicitly allowed for each state remain available. For example, `awaiting_feedback → awaiting_approval` requests approval; it does not grant approval. `apps/api/app/worker_transitions.py` defines the complete Worker edge list.

Mock completion requires `virtualization=mock`, no central `DeliveryJob`, and the latest PR approval audit. Its append-only organization/work/repository/correlation scope, Web/Slack transport, approver roles, approval decision, delivery-not-queued flag, absent bundle hash, before/after statuses and integer version must match the current progress. An unrelated console approval does not hide valid PR approval. Relabeling a Worker as Mock cannot take over completion when a central job exists. Existing approval/bundle/job checks for actual SCM delivery are unchanged.

## API example and compatibility

For work at `awaiting_approval`, version 6, with a valid work lease:

```json
{"status":"committing","expected_version":6,"payload":{"approved":true}}
```

The response is `403` with `{"detail":"worker cannot perform this transition"}`. Status/version/events/approvals/audits/jobs remain unchanged, and lease renewal within the rejected request rolls back. Missing/invalid leases still return `401` first; stale versions or graph-invalid transitions still return `409` before graph-valid but unauthorized Worker transitions return `403`.

An authorized user can send `{"kind":"pull_request","decision":"approve"}` to the existing `POST /api/work-items/{id}/approvals` and receive `200` with the `committing` work. The normal Mock client observes that state before reporting `pr_created` and `completed`. Actual delivery proceeds through the central job.

Clients that previously used a lease to perform user transitions now fail and must use the user API with its existing role/Origin checks. Request/response schemas, environment variables, dependencies and database migrations are unchanged. Legacy Mock intermediate work without the required audit also fails closed. Do not backfill/edit approval audits or automatically reapprove already-committing work. Use an authorized operational recovery procedure or a new Mock work item where necessary.

## Verification — 2026-09-09

- On pre-fix `cb6c561`, 39 of the initial 74 specifications failed because they received `200` instead of the required `403`; 35 passed. These results were repeated against unchanged old code after correcting a fixture setup error.
- The final 77 specifications cover all 37 common graph edges, authority/version-check precedence, complete database invariance on denial, authorized user resumption/Mock completion, invalid audit/central-job/different-assignment cases and future graph expansion.
- Actual Uvicorn/SQLite, disposable OIDC sessions, two organizations and scoped leases exercise feedback resumption, Mock completion after PR approval and implementation after budget approval. Tests check approver/actual-state events on a previously opened SSE stream, append-only audits, operator approval/audit-read denial, foreign-organization 404 and wrong-Origin 403. This is not external IdP login or actual VM execution. Synthetic credentials must not appear in API logs/database bytes or failure representations. The focused run including this HTTP test and the failure-representation test passed 79 tests.
- Six additional PostgreSQL cases exercise real JSON audit queries/locking: approved, missing, stale, rejected, central job and foreign repository. Final `make test` against a dedicated PostgreSQL database passed API 1,160 (215.24 seconds, no skips), Runner 28, Worker/Gateway, and Web 91 plus type checking. `make test-api`, `make lint`, the production Web build and 33 Chromium E2E tests (2.0 minutes) also passed.

New tests run in the existing `Python` CI and `test_worker_postgres.py` step. The six PostgreSQL cases skip locally without `KELPIE_TEST_POSTGRES_URL` and require separate verification. No CI job, eight-minute timeout, dependency or security setting changes are needed.

## Actual screens and limitations

An isolated actual API/Next.js/scoped Mock Worker was used through the embedded browser: create work → remain awaiting approval → user feedback → reverify → user approval → completion/resource release. This screen environment explicitly uses `AUTH_MODE=development`, separately from the OIDC HTTP test above. Korean and English 390px views had 375px document width without horizontal overflow and showed 100% completion, Live and closed feedback. A Computer-use screenshot also confirmed Korean completion and resource-release events in the actual target window. Native keyboard/mouse input remains unverified because OS focus is unsupported. UI source is unchanged.

The observed network interval contained 38 requests: 36 with 200, one creation with 201 and one static-cache 304; no external requests or browser console messages. Earlier development-server startup logs separately showed a favicon 404 and a blocked development CSS request with an unknown origin. Security settings were not relaxed. The full desktop remains private and public-sharing permissions were unchanged. Owned pages/terminals/services were closed, and ports 13100/18100 were confirmed free after E2E shutdown.

This change enforces authority over control-plane state. It does not prove arbitrary `/events` provenance, physical compute termination/time-budget enforcement in an untrusted VM, actual SCM delivery, HTTPS OIDC browser mutations or dual-KVM isolation. Nor does it imply an actual SCM delivery bypass was reproduced. Those boundaries and remaining MVP gates require separate verification.

Rollback uses a normal revert with no migration. The previous API restores Worker access to user-owned transitions, so assess that risk. Do not disable authentication/audit/approval checks or rewrite historical audits/events.
