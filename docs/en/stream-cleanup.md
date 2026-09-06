# Live-stream shutdown and database connection cleanup

English | [한국어](../ko/stream-cleanup.md)

## Cause and behavior

After PR #34, the create-failure/retry scenario passed on `main` CI, but API logs contained connection-termination errors and SQLAlchemy unchecked-in connection warnings. Two regression cases cancelling real SQLite queries reproduced the same failure. Inspect actual service shutdown logs, not just test counts.

Each SSE batch shields authentication/authorization revalidation, event reads and session cleanup from AnyIO's repeated external cancellation. An internal `asyncio.timeout` sets a two-second read deadline. Pending cancellation is checked again immediately after the batch. Neither event yields nor idle waits execute inside the shield. [AnyIO cancellation and shielding](https://anyio.readthedocs.io/en/stable/cancellation.html), [Python asynchronous timeouts](https://docs.python.org/3/library/asyncio-task.html)

- Initial access checks and per-batch OIDC session/organization/repository revalidation remain. Cross-organization access still returns 404; revoked access ends the stream.
- After streaming starts, read timeouts or SQLAlchemy errors end the stream with the fixed sanitized warning `event stream read failed; closing stream`. No SQL, parameters, connection details or fake success event are emitted. Clients retain their existing connection-recovery policy.
- Normal `GET /api/work-items/{work-id}/events?after=0` retains `200 text/event-stream`, `id: <event-id>` and `data: <EventView JSON>`. Event shape/order, 100-row batches, keepalives and cursor contracts are unchanged. An already-sent 200 is not replaced with a late 5xx.
- Two seconds is the read cancellation deadline; driver cleanup can add time. This does not guarantee forced shutdown of an unresponsive driver. Initial request-session occupancy during an active stream and cursor-range validation are separate concerns; this change verifies returns after disconnect.

## Operations and CI

No new environment variables or database migrations. Declare `anyio>=4,<5` as a direct API dependency. FastAPI/Starlette already use it; direct usage is needed for AnyIO's repeated-cancellation boundary, which standard `asyncio.shield` alone does not address. Install dependencies with API rollout. Rollback can revert the logical commits and restart the API, but restores the earlier leak risk.

`make test-api` includes SQLite and actual HTTP verification. Run the six PostgreSQL cases with a dedicated `KELPIE_TEST_POSTGRES_URL` and the following command. Missing URLs skip those tests and do not count as PostgreSQL verification. Cleanup removes only each test's UUID schema.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_stream_cleanup.py -k postgres
npm run test:e2e --prefix apps/web -- stream-cleanup.spec.ts
.venv/bin/python -m ruff check apps/web/e2e/stream-runtime.py
```

Existing required `Python` CI adds one PostgreSQL step; existing `Web` E2E runs an actual Chromium `EventSource` case. No new job, matrix or timeout increase. Test-only query barriers/counters exist only when explicitly running `apps/api/tests/stream_runtime_app.py`, not the production `app.main:app`. Only credential-bearing traces are excluded for the new browser case; page-error assertions, failure screenshots, shared resource-release checks and service-shutdown log assertions remain.

## Verification · 2026-09-06

Runtime code, tests and CI commit: `35a29a2`.

- Twelve SQLite/PostgreSQL cases pass: cancellation during authentication/event reads/rollback, read timeout/database errors followed by reconnection, and idle shutdown. The fixture restores loggers disabled earlier in the full suite so log assertions genuinely run.
- Actual Uvicorn/SQLite queries are temporarily held while closing HTTP connections eight times. Every cycle verifies zero active streams/checked-out connections, followed by real event delivery, cross-organization 404 and shutdown-log inspection.
- Actual Chromium closes `EventSource` during the same query four times, checking the query is still running, connection return after every cycle, a fresh real `work.created` event, final return and shutdown logs.
- Dedicated PostgreSQL 17 `make test`: API 687 (no skips, about 88 seconds), Runner six, Worker/Gateway, Web 52 and TypeScript pass. `make lint`, Python browser-helper Ruff, `pip check`, production Web build and ten monitoring rules pass. All 20 Chromium cases pass in about 1.6 minutes without connection-leak errors or tracebacks. Existing Next.js smooth-scroll configuration warnings remain separate UI follow-up work.
- Actual Uvicorn `:18530` and Next.js standalone production `:13530`: Orca Korean/English detail pages, a new lease-authenticated event, list navigation, reconnection and reload. Final started/closed streams: 5/5; active streams/checked-out connections: zero. Before cleanup, console messages: zero; HTTP errors among 65 captured requests: zero.
- Computer Use inspected actual Korean/English 1035px screens and event content. Device emulation reported 390px but actual width remained 1035px, so this is not manual mobile evidence. Narrow-layout checks are included in the full Chromium suite. This is not real IdP/VM/SCM or native OS input verification.

Production UI code is unchanged, so before/after UI screenshots are not applicable. Desktop captures containing other projects are not published. Final head CI and merge SHA are recorded in the PR. Cache protection, artifact-viewer UX, physical file recovery/retention and real KVM verification remain; the full MVP is incomplete.
