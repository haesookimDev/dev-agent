# Feedback by work state — 2026-09-05

English | [한국어](../ko/feedback-lifecycle.md)

## Behavior and compatibility

Delivery states `committing`, `pr_created` and terminal states `completed`, `failed`, `cancelled` no longer accept feedback. Web and signature-verified Slack commands apply the same check while holding the work lock. Existing organization/repository authorization and Worker quarantine checks remain in place.

Existing acceptance remains unchanged for `queued`, `provisioning`, `analyzing`, `implementing`, `verifying`, `awaiting_feedback`, `awaiting_approval`, `awaiting_input`, `budget_exhausted`. Only the three `awaiting_*` states resume `implementing`; feedback does not automatically renew the budget or resume budget-exhausted execution.

The request below now returns `409`, instead of the previous `200`, during delivery or after termination. Existing clients must refresh the work state instead of reporting success. Rejection does not change Feedback, events, work version or update timestamp.

```http
POST /api/work-items/WORK_ID/feedback
Content-Type: application/json

{"message":"Request further changes","channel":"web"}
```

```json
{"detail":"work no longer accepts feedback"}
```

The same restriction applies to Slack `/kelpie feedback WORK_ID message`. Authorization denials and hidden targets retain `403` and `404`, and Worker quarantine retains its existing `409` explanation. No schema, environment variable or database migration changes.

## Interface

Closed work replaces the send button with guidance to review activity/evidence and request further changes as a new work item. If another window approves or the work ends while typing, the draft stays in a read-only input on the same page. Keyboard selection/copy and the focused input are preserved. Drafts are not stored on the server or in browser storage; copy them before refreshing or navigating away.

A stale submission rejected with `409` refreshes the work and shows an unsent notice. Starting a new draft clears the previous success notice so unsent content cannot be mistaken for sent feedback. Korean and English are synchronized.

## Verification

- Before the fix, ten regression cases failed: Web/Slack × five closed states.
- `make test`: API 229 (including real PostgreSQL locking tests, no skips), Runner 6, Web 30 plus type checking, Worker/Gateway Go tests passed.
- `make lint`, `npm run build --prefix apps/web`: passed.
- `npm run test:e2e --prefix apps/web`: twelve passed on the integrated code, final local run about 1.1 minutes. Each invocation provisions a fresh API/database and single-slot Mock Worker.
- After real API/Worker approval and completion, a browser with its SSE stream disconnected receives 409 for late feedback and retains a selectable/copyable draft. Live closure also verifies focus/draft preservation and removal of the old success notice.
- Five closed states × both locales, 390px overflow and evidence links additionally use explicit presentation response fixtures. These are not proof of actual failed/cancelled execution.
- In a separate local SQLite API + two scoped-auth Mock Workers + production Web build, used the Orca browser to create work, send valid feedback, re-verify and enter a new draft. Approved the same work in English using Chrome Computer-use, then directly checked SSE closure, draft preservation and removal of send controls in Korean.
- The final real work is `completed`, version 12, with one accepted feedback event. Late feedback returns 409 without changing work/events, and both Workers have all resources restored. No normal-flow browser console errors or request/page errors in separate English/390px Chromium renders.
- Hands-on use exposed a stale success notice. Fixed it, rebuilt the production Web bundle and repeated the cross-browser journey.
- The initial PR CI passed, but log review exposed five Korean time hydration errors, so merging was paused. Merged and integrated the separate [time formatting fix and global browser error guard](time-hydration.md) first. Every E2E now fails on unhandled page errors.
- After full verification on integration commit `69958be`, rebuilt the production bundle and isolated runtime. Repeated creation, feedback, re-verification, new draft, approval and completion in Orca, then checked the completed English view in actual Chrome. Final images and state/event/resource assertions come from this integrated run; Korean timestamps match browser-local formatting.

Before/after images use different synthetic work items. The before image reuses the existing dashboard verification's completed view. The after draft view was captured directly in Orca; English/390px images render the same production Web build in a separate Chromium browser.

![Before: send controls remain after completion](../assets/dashboard-ux/detail.jpg)

![After: closure guidance and unsent draft](../assets/feedback-lifecycle/completed-ko.jpg)

[English](../assets/feedback-lifecycle/completed-en.jpg) · [390px](../assets/feedback-lifecycle/mobile-ko.jpg)

## Scope and follow-up

No new dependencies or changes to authentication/approval policy. Roll back the related feature commits if necessary, noting that this re-enables ineffective feedback after termination. Actual GitHub delivery, IdP, VM, WireGuard and noVNC operation are outside this Mock verification.

Misleading 100% progress for failed/cancelled work was fixed in a [separate follow-up UI change](terminal-progress.md). Remaining documented MVP items (immutable audit, preview authorization, secret checks, operational readiness and real VM verification) still require work; this change does not complete the MVP.
