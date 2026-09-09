# Runner resumption after user decisions

[한국어](../ko/runner-resumption.md) | English

## Execution contract

The Runner's waiting loop uses the current `status` from `/api/runs/{id}/commands` to decide whether to execute. Feedback/approval rows are history, not execution authority. This complements [Worker transition authorization](worker-transition-authority.md); the Runner does not create approval on behalf of a user.

| Observed state | Runner behavior |
| --- | --- |
| `implementing` | Resume implementation and independent verification authorized by the user decision |
| `budget_exhausted`, input/feedback/approval waits, or any other state | Wait without executing; retain the unread feedback cursor |
| `committing` | Emit `delivery.ready` and exit; delivery belongs to the control plane |
| `pr_created`, `completed`, `failed`, `cancelled` | Exit before processing stale feedback |

Budget approval or PR rejection that changes the state to `implementing` resumes work even without new feedback. An already-advanced approval-history cursor does not prevent resumption when the current state authorizes it. Pending feedback is applied in order, together with the latest independent verification failures when applicable. Repair limits, evidence/patch uploads after successful checks, approval waiting, failure reporting and session cleanup remain. Server event messages now use `Authorized` instead of feedback-only wording to describe budget resumption accurately; status and event-type contracts are unchanged. UI status translations are unchanged.

No API schema, user authorization, Origin check, database migration, production default, dependency or environment-variable changes are required. Updating the Runner applies the change; running processes are not automatically replaced and historical work is not forcibly resumed.

## Verification — 2026-09-09

- On pre-fix `52e56c5`, 14 of 17 new specifications failed and three passed. Scripted control-plane states reproduce execution from feedback before approval, stalling after budget approval/PR rejection without feedback, and feedback taking precedence over terminal states.
- `6867f18` passed those specifications, all 45 Runner tests and `make lint`. Coverage includes pending cursors, fail-closed future waiting states, bounded repairs/failure context across repeated budget approvals, and exception cleanup.
- `b409a3d` adds two actual HTTP regressions using Uvicorn/SQLite, disposable OIDC sessions, scoped leases, a separate Runner process, local Git cloning, independent Python checks and uploaded patch files. Only model turns are scripted; server state/authorization checks are real. The flow is: first check fails → budget exhaustion → with/without feedback, two further polls still retain one turn and the version → administrator budget approval → passing checks → PR rejection without feedback → third turn/passing checks → user PR approval/delivery-ready/exit 0.
- Tests verify operator budget-approval 403, foreign-organization 404, approver audit identity/decision order, actual verifier exit codes `1, 0, 0`, the third revision in the final patch, and absence of synthetic credentials from API/Runner logs, events and database bytes. An initial test-only patch-column mistake was corrected to verify the actual file path/bytes; it is not counted as product regression evidence.
- Automated regression shortens only the fixture process's poll wait to 50ms; hands-on use separately runs normal three-second polling. This is not actual model/IdP/SCM/VM execution or time-budget enforcement. The new regressions run in existing `make test-api`/required `Python` CI without another job, dependency or change to the eight-minute limit.

Final verification passed 19 focused tests; `make test` against a dedicated PostgreSQL database passed API 1,162 (223.00 seconds, no skips), Runner 45, Worker/Gateway and Web 91 plus type checking. `make lint` and the production Web build passed. The existing 33 actual Chromium/API/Next.js/scoped Mock E2E tests passed in 2.0 minutes. That development-authentication/Mock Worker journey is distinct from the new Runner HTTP regressions.

## Actual screens and limitations

A production Web build was connected to the actual OIDC API/Runner fixture. A bounded one-time local cookie endpoint installed a synthetic session without printing secrets. User feedback, budget approval, PR rejection and approval were performed through separate authenticated HTTP with the correct Origin. The local HTTP page differs from the required HTTPS OIDC origin, so this is not verification of browser mutations or external IdP login. Authentication, TLS and system trust were not relaxed.

Without refreshing, the screen reflected: budget exhaustion/version 6/one turn after feedback; approval waiting/version 9/255 minutes/two turns after budget approval; `{"turn":2}` in the `revision.json` preview; version 12/three turns after PR rejection; and delivery-ready/session closure/version 13/process exit 0 after final approval. Korean/English 390px screens had 375px document width with no horizontal overflow, showing `커밋 중`/`Committing` at 75%, Live and closed feedback. Actual delivery is not executed by this synthetic environment; work was not manipulated into completed/PR-created status.

All 55 requests in the observed browser interval returned 200, with no external requests or console messages. An initially incorrect standalone launch path was corrected to the actual generated path before verification. A browser-observation connection error was followed by rechecking the existing services/tab and continuing. Embedded-browser screenshots in both languages were verified, but Computer-use captures showed another workspace and are not evidence of the target native display/input. Escape did not demonstrably close the dialog and is not recorded as successful input. No UI or native feature source changed.

The full desktop remained private and public-sharing permissions were unchanged. Owned pages/terminals/API/Runner/Web and disposable data were cleaned up, and ports 13100/18100/13500/18500/18501 were confirmed free. The production build restored generated Web type paths; no uncommitted UI changes remain.

## Follow-up and rollback

Hands-on inspection confirmed that the exhausted-budget page has no budget-approval button. This is a separate user-control UI follow-up to the API/Runner resumption behavior. Cancellation races after command observation, physical termination/time-budget enforcement in an untrusted VM, actual KVM/Preview isolation and overall MVP completion require separate verification.

A normal Runner revert needs no migration, but the old Runner can execute before approval and stall after authorization. A Worker API 403 cannot stop a model turn already started; assess that rollback risk and retain the API's approval boundary.
