# Route scroll verification

[한국어](../ko/navigation-scroll.md) | English

## Behavior and scope

Global `html` retains `scroll-behavior: smooth`; the locale layout now declares `data-scroll-behavior="smooth"`. Following the installed Next.js 16.3.4 guide, the router temporarily uses instant scrolling during route transitions and restores the original setting. Existing in-page anchors and `prefers-reduced-motion: reduce` using `auto` remain. Layout, colors, copy, API, authorization, dependencies, environment variables, CI jobs and timeouts are unchanged.

## Automated verification — 2026-09-09

The configuration warning appeared in PR #42's actual CI logs. The regression failed twice with the same warning on pre-fix code `b2c99fb`. An initial incorrect navigation label in the test was fixed separately; that failure was not counted as product regression reproduction.

`navigation-scroll.spec.ts` uses actual disposable API/SQLite/Mock Worker services and Chromium, without replacing requests with mocked responses. Four combinations of Korean/English and normal/reduced motion check:

- Returning from the bottom of a long work detail to the dashboard, top restoration, no warning, the HTML contract and removal of temporary inline overrides.
- Re-entering detail, browser history, the in-page new-work anchor and no horizontal overflow at 390px.
- Actual browser focus/Enter on the skip link, resulting main-content focus and preservation of the motion setting.

Implementation `9f3153d` passed `make test-web` (91 tests and type checking), `make lint`, production build and the full `npm --prefix apps/web run test:e2e` suite (33 tests, 1.9 minutes). The subsequently strengthened anchor/skip-link checks passed all four combinations (14.1 seconds). Required Web CI reruns the full suite on the final PR head. Screenshots use the existing seven-day `browser-evidence` artifact without an extra upload job.

Local `make test-api`, `make test-runner`, `make test-worker`, `make test-gateway` and full `make test` were not rerun for this Web-only change. Those implementations are unchanged and passed the dedicated PostgreSQL full suite and post-merge CI for PR #42. Existing required Python/Go CI still runs for the new PR.

## Hands-on verification and limits

Ran commit `9f3153d`'s production build using the actual standalone server. After observing the standalone warning from `next start`, stopped that process, staged generated static assets and restarted through `server.js`; only the final launch is used as acceptance evidence. The API/Mock Worker used an owned disposable fixture, without SCM credentials or real VMs.

In the Orca browser, English work detail at scroll 403px returned to dashboard at 0px, retaining `smooth` with no inline override. The Korean 390px page had no horizontal overflow and its new-work anchor scrolled to 821px. The final Korean screen was visually inspected in an actual computer-use desktop capture. All 30 captured requests returned 200, with no external requests or console messages.

Orca skip-link input did not produce a verified target hash/focus change, so it was not counted as successful. The same function passed the four actual Chromium keyboard cases above. OS window focus was unsupported; native keyboard/mouse input was not verified. Captures showing another workspace were excluded, and the correct workspace was selected before final visual verification.

A before/after HTML comparison containing only synthetic test data was retained locally. Device settings denied Orca public artifact sharing; stopped after one denial without changing permissions or retrying. The PR links existing GitHub CI before/after evidence. Whole-desktop captures are not published.

Stopped owned services and browser/terminal tabs and verified no listeners on ports 13100/18100. Fixture lifecycle cleaned its database, credential file and Mock workspace; existing user terminals/services were unchanged.

## Maintenance and rollback

Preserve these regressions when changing routing, global scrolling, anchors or accessibility; do not merely suppress warnings or remove reduced-motion handling. Static before/after screens demonstrate preserved design, not animation timing itself. To revert, restore the layout attribute and related expectations together, rerun Web verification and build. No data migration or configuration change is needed.
