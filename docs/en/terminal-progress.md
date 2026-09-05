# Progress for stopped work — 2026-09-06

English | [한국어](../ko/terminal-progress.md)

## Change

Removed the 100% value and full progress bar from `failed` and `cancelled` work. The status badge remains, with an explanation that the work did not complete and guidance to review activity. Narrow layouts consistently place the explanation below the status. Korean and English are synchronized.

Successful completion remains 100%; existing stage values, feedback closure and draft preservation are unchanged. Only the presentation helper `statusProgress` now returns `number | null`, with all callers updated. No changes to API states, database, approvals, authentication, configuration or dependencies.

## Verification

- Before the fix, two failed/cancelled value regressions and four Korean/English rendering regressions failed. Also reproduced the narrow-layout inconsistency found during hands-on review as a failing E2E before fixing it.
- `make test-web`: 40 tests and type checking passed. `make lint`, `npm run build --prefix apps/web`: passed.
- `npm run test:e2e --prefix apps/web`: 12 passed, final local run about one minute. Retains real API/single-slot Mock Worker creation, feedback, approval and completion, plus existing error, authorization, SSE and time regressions. State-specific presentation uses explicit response fixtures.
- Ran a separate SQLite API and production Web bundle (`13520`/`18520`). Created work in Orca, then transitioned it through test Worker protocol calls to failed/cancelled. Compared the failed view across before/after page loads; the cancelled work changed live through SSE from 0% to the stopped-work explanation without a bar.
- Used Chrome Computer-use to open the English failed view and check its explanation and closed controls. Native screenshots containing personal tabs are not published.
- After the mobile adjustment, recaptured Orca on the `342e923` production bundle and rechecked twelve combinations in independent Chromium: Korean/English × 1440px/390px × failed/cancelled/completed. Verified stopped guidance, completed 100%, narrow vertical layout, no horizontal overflow, and first keyboard focus/indicator. No normal-flow page/request errors or Orca console errors.
- The initial completed-bar E2E read width zero before rendering as its expected value. Corrected the measurement to retry comparison of the two rendered widths without increasing timeouts or weakening checks.

This Web-only change did not separately run local `make test-api`, `make test-runner`, `make test-worker`, `make test-gateway` or `make test`. PR CI retains full component and PostgreSQL checks. Test protocol calls and Mock journeys are not evidence of actual VM failure/cancellation, GitHub delivery, IdP, WireGuard or noVNC operation.

## Screens

Korean before/after images directly capture the same synthetic failed work in Orca. English/390px images use independent Chromium on the final production bundle and also show keyboard focus on the skip link.

![Before: failed work claims 100%](../assets/terminal-progress/before-ko.jpg)

![After: failed status and guidance](../assets/terminal-progress/after-ko.jpg)

[English](../assets/terminal-progress/after-en.jpg) · [390px cancelled view](../assets/terminal-progress/mobile-ko.jpg)

## Impact and follow-up

Reverting the feature commits rolls back the change but restores misleading 100% displays for failed/cancelled work. No data migration. Immutable approval audit, preview authorization, secret non-disclosure, operational readiness/retention and real KVM isolation remain required MVP work.
