# Time-budget approval for a reviewed version

English | [한국어](../ko/budget-approval-version.md)

## Contract and client migration

`kind=budget` on `POST /api/work-items/{id}/approvals` requires top-level `expected_version` for both approval and rejection. Send the WorkItem `version` the user reviewed as a **positive JSON integer**. For example, extending exhausted work at version 4 by 15 minutes uses:

```json
{"kind":"budget","decision":"approve","expected_version":4,"payload":{"minutes":15}}
```

Success returns the existing WorkItem JSON (200). Starting at 240 minutes, this example returns `status=implementing`, `version=5`, `budget_minutes=255`. Rejection records the decision without changing budget, state or version. A new approval after rejection of the same version therefore remains possible; this is not an idempotency key for every decision.

Omitted/`null` versions, booleans, strings, decimal notation, nonpositive integers, arrays and objects return 422. Missing/`null` budget versions return `{"detail":"expected_version is required for budget decisions"}`; other invalid types use the standard input-validation response. Old or future versions return 409, for example `{"detail":"version mismatch: current version is 7"}`. Failed requests leave budget, work, approvals, activity, audits and delivery reservations unchanged. Permission, organization and Worker quarantine boundaries remain enforced. For correctly typed requests, authorization precedes version/state checks.

This is a **breaking change for existing budget-decision requests**. Update callers to fetch work and retain the version whose state and extension amount were shown to the user. After a 409 or lost response, fetch current work and let the user review the outcome. Do not automatically replace the version and resend old intent. Existing PR/console approval requests omitting the version continue to work. Slack PR approvals and Worker command formats are unchanged. Budget activity events gain the reviewed version; existing audit before/after version fields remain.

The 15–1440 integer-minute extension, omitted 60-minute default and Approver permission remain unchanged. No database migration, new environment variables or dependencies are needed. Coordinate API and budget-caller deployment. Rolling back to the previous API removes version enforcement; avoid budget approvals and recover with a forward fix. Do not delete audits or disable approval gates to recover.

## Regression and hands-on verification · 2026-09-09

Implementation `b5c92ac`, actual HTTP/concurrency regressions `d61cc5f`, CI `ebd9346`. Subsequent documentation does not change runtime code.

- Before the fix, 24 of 30 new tests failed and six passed. Afterward, 152 tests including existing permission, audit and Runner regressions passed. Separate actual OIDC-session/scoped-Worker HTTP coverage verifies exhaustion→approval→exhaustion→old approval/rejection refusal→current-version rejection/approval and retained audits. No external IdP, VM or SCM is used.
- Four PostgreSQL cases passed: reading the latest version after waiting for a row lock, approval after rollback, concurrent duplicate approval and audit-failure atomicity. Together with the four existing cancellation/Claim cases, eight passed in 3.24 seconds. Existing required `Python` CI runs these in its existing PostgreSQL step, without new jobs, matrices or timeouts.
- Dedicated temporary PostgreSQL `make test`: API 1,197 (223.52 seconds, no skips), Runner 45, Worker/Gateway and Web 91 plus type checking passed. `make lint` passed. Existing Web E2E/build and latest-head CI results are recorded in the PR.
- Actual isolated API `:18500` (OIDC/scoped) and unchanged production Web `:13500` showed budget 240/v4→valid approval 255/v5→Worker exhaustion 255/v7. Replaying the old approval returned 409 with every retained row unchanged. Only the newly reviewed v7 extended to 270/v8, with two audit rows. Omitted version returned 422 without mutation. Approval HTTP was sent through the isolated terminal with a valid Origin, not a browser approval button.
- Inspected final Orca browser screens in Korean at 1100×900 and English at 390×844, live state/budget/version, no horizontal overflow, 64 observed network requests all returning 200, no external requests and no console errors. An initial 404 at the single-use login entry was resolved by navigating to the actual page with the established session. A duplicated-tile 1440px capture was excluded from final evidence and observation was repeated.
- Computer-use captured the desktop, but another workspace was visible and target-window focus could not be acquired. Input remained unverified after the restore attempt; this is not claimed as native-screen/input success. This PR changes neither UI nor native input implementation.

Stopped the test API, Web and browser, and removed only their owned temporary data. API shutdown-log and database credential-leak checks passed; existing user processes and databases were preserved.

Budget-extension UI is the next separate task. Physical VM time enforcement, active-run cancellation and post-observation execution races are not solved by this version check. Actual Linux/KVM/WireGuard/noVNC concurrent-run acceptance remains outstanding.
