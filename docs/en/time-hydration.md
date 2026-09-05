# Time hydration verification — 2026-09-05

English | [한국어](../ko/time-hydration.md)

## Cause and fix

The feedback PR's first [CI run](https://github.com/haesookimDev/dev-agent/actions/runs/33970545553) passed, but its log contained five Korean time mismatches: server `PM 2:03:03` versus browser `오후 2:03:03`. Paused merging and separated this time-rendering fix into an independent change.

Matching the server/browser timezone to UTC does not guarantee identical strings from their locale data. SSR and initial hydration now use locale-independent `HH:mm:ss UTC` or `YYYY-MM-DD HH:mm UTC`. Only after the browser is ready do they format Korean/English in the browser's local timezone. Original `dateTime` and UTC `title` attributes remain unchanged. Hydration warnings are not suppressed.

## Verification

- `make test-web`: 15 tests and type checking passed. Three regression cases with altered server locale formatting failed before the fix and passed afterward.
- `make lint`, `npm run build --prefix apps/web`: passed.
- `npm run test:e2e --prefix apps/web`: deliberately different browser/server locale formats in Korean and English, with Honolulu timezone, verify list dates, event times and no hydration errors. Existing real API/Mock Worker journeys remain.
- A shared E2E fixture checks unhandled `pageerror` events in every test. A temporary probe raised a real error and confirmed that the fixture itself failed; the probe was then removed. Intentional network-failure scenarios still pass.
- In a separate SQLite API + scoped-auth Mock Worker + production Web build, captured the same work before hydration, before/after the change. Blocked bundle requests while allowing streamed HTML's reveal script to execute. Those bundle failures are an intentional capture condition.
- In normal Orca Korean rendering, confirmed `14:08:23 UTC` becomes Seoul local time `오후 11:08:23`, with no console errors. In Chrome Computer-use, visually verified English `11:08:23 PM`, approved the work and confirmed completion/resource release.
- Separate Chromium renders at English 1440px and Korean 390px compared all 15 event times to browser-local formatting. No request/page errors or horizontal overflow.

API/Runner/Worker/Gateway code is unchanged, so their local tests were not repeated for this unit. Normal GitHub CI retains all component checks. The executor is Mock; this does not replace real VM/SCM delivery acceptance.

## Before and after

Both images show the same work **before browser localization**. The normal hydrated design/time format is unchanged. A UTC string may be briefly visible during initial rendering.

![Before: server-locale-formatted UTC time](../assets/time-hydration/before-ssr.jpg)

![After: identical UTC text in both runtimes](../assets/time-hydration/after-ssr.jpg)

[Hydrated Korean 390px view](../assets/time-hydration/hydrated-ko.jpg)

No new dependencies, configuration, API contracts or database migration. UTC is a locale-neutral standard abbreviation applied to both languages. Rolling back can reintroduce hydration recovery caused by differing runtime locale data.
