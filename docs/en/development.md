# Development and verification

English | [한국어](../ko/development.md)

## Artifact cache regression

[Artifact HTTP cache protection](artifact-cache.md) runs in existing `make test-api`/`Python` and `Web` CI. Verify `no-store`/`Vary: Origin` on lists, files and handled errors, actual HTTP headers, and Chromium navigation → restoration → same-URL fetch and access changes with its default cache. Do not substitute cache disabling, URL nonces or mocks. Lint the browser helper with `.venv/bin/python -m ruff check apps/web/e2e/artifact-cache-runtime.py`. No CI job or timeout is added.

## Stream shutdown regression

[SSE connection cleanup](stream-cleanup.md) adds `python -m pytest -q apps/api/tests/test_stream_cleanup.py -k postgres` to existing required `Python` CI alongside SQLite, actual HTTP and Chromium coverage. Locally, a dedicated `KELPIE_TEST_POSTGRES_URL` is required; without it six cases skip. Verify actual disconnects during queries, returned connections, reconnection and shutdown logs. Passing test counts do not excuse connection-termination errors or unchecked-in connection warnings. Lint the browser helper with `.venv/bin/python -m ruff check apps/web/e2e/stream-runtime.py`. Existing CI jobs and eight-minute limits remain.

## Artifact filename regression

[Filename/header boundaries](artifact-filenames.md) run in existing `make test-api`/`Python` CI, covering international names, controls and safe fallback for retained names. The `Web` content case also asserts filenames chosen by actual Chromium downloads and original bytes. Do not replace this with only header-string checks or forced download names. Existing content, authorization and page-error checks remain; no CI job is added.

## Artifact content regression

[The content execution boundary](artifact-content.md) checks registration/download formats, actual bytes and security headers in `make test-api` and existing `Python` CI. Existing `Web` CI runs `artifact-content.spec.ts` against real HTTP services to verify inert text, loaded images, JSON and rejection of retained HTML. Only this case excludes traces containing temporary scoped-auth headers; page-error assertions, failure screenshots and shared resource-release checks remain. Also lint its Python service with `.venv/bin/python -m ruff check apps/web/e2e/artifact-runtime.py`. No job or dependency is added.

## Work-scoped artifact regression

[Artifact storage boundary](artifact-isolation.md) file/API/real HTTP tests run in `make test-api` and existing required `Python` CI. Both registration and downloads must enforce the exact work's `artifacts` namespace without retained-metadata or descendant-link bypasses. Restore fixtures also use valid work-scoped keys so missing-file rejection remains verified.

## Delivery-byte integrity regression

[Patch integrity](delivery-integrity.md) file/approval/delivery tests and real HTTP/Git regression run in `make test-api` and existing required `Python` CI. Delivery changes must retain rejection of missing/corrupt/out-of-root files and application of approved bytes even after original-file replacement. Use fixtures matching actual files instead of bypassing invalid test hashes.

## PostgreSQL restore regression

[Backup/restore verification](postgres-restore.md) runs `test_postgres_restore.py` and `test_postgres_restore_runtime.py` with dedicated `KELPIE_TEST_POSTGRES_URL` and `KELPIE_TEST_POSTGRES_CONTAINER` settings pointing to the same server. Existing `Python` CI reuses its PostgreSQL 17 container's tools. Locally, missing variables skip six tests; passing default tests alone does not complete restore verification. Cleanup removes only test-created UUID databases/roles, never existing databases or audits.

## Definition of done

Develop on a task branch and commit each logical unit with a Korean message after its relevant tests pass. Once automated tests and hands-on verification pass, create a PR with evidence and inspect CI and reviews for the latest commit. When the user delegates merging, the agent merges with a merge commit and fast-forwards local `main`. Keep a PR in Draft when required verification cannot run.

Continuous MVP development uses the roadmap's [next release](roadmap-summary.md#immediate-next-release) as its acceptance boundary. Do not automatically include P3 and later expansion work. Merge delegation for the current MVP does not authorize production deployment, paid infrastructure, bypassing branch protection, or removing the product's user-approval gates.

## Verification

- `make test`: API, Runner, Worker, Gateway, Web tests and Web type checking
- `make lint`: Python Ruff, Go vet and Web ESLint
- `make test-monitoring PROMTOOL=/path/to/promtool`: monitoring configuration and PromQL alert validation. [Installation and operations](monitoring-alerts.md)
- `cd apps/web && npm run build`: production Web build
- Database changes: real PostgreSQL upgrade, schema comparison and safe rollback
- User-facing features: run the services, perform the affected browser journeys, and check Korean/English and desktop/mobile widths

Prioritize work status and next actions in the UI. Check empty/error states, keyboard navigation, focus, contrast and long content. Use Browser/Computer-use to inspect the final screens and console/network errors, and preserve core journeys as repeatable E2E tests. Native console-input changes also require actual desktop interaction. Attach before/after screenshots without secrets to the PR.

Isolate test environments and clean up only resources created for the task. Do not describe Mock Worker success as real Linux/KVM acceptance. For documentation-only changes with nothing to run, record that runtime verification is not applicable and explain why.

### Browser regression tests

Install the API development environment (`.venv`), Go and Web dependencies, then run from the repository root:

```sh
npm ci --prefix apps/web
npx --prefix apps/web playwright install chromium
npm run test:e2e --prefix apps/web
```

Playwright runs actual Chromium, Next.js, API and Mock Worker processes. Each run migrates a temporary SQLite database, uses a random test Worker credential and cleans up only its own processes and data. Keep ports `13100` (Web) and `18100` (API) free. Stop any `next dev` instance in the same checkout first. Set `KELPIE_E2E_PYTHON` only to override `.venv/bin/python`. On Linux, use `playwright install --with-deps chromium` to install system libraries too.

Coverage includes creation, live events, feedback, re-verification, approval and resource release; search, filters, language and narrow layouts; network/permission failure retries; stream recovery; and invalid work addresses. The test Worker has one execution slot; every test uses the automatic resource-release fixture from `e2e/fixtures`. Failures preserve screenshots and traces in `apps/web/test-results`. The HTML report in `apps/web/playwright-report` is generated and must not be committed. A production build after E2E also regenerates the development-updated `next-env.d.ts` for production.

[Unassigned queued cancellation](work-cancellation.md) additionally covers confirmation, Esc/focus, duplicate submission, permission denial, version conflicts, lost success responses, and live SSE changes. Work cancelled without execution history must have no lease and emits no release event. Existing release assertions for executed work remain unchanged.

Playwright is a development dependency for real browser/service contracts that unit tests cannot exercise. No runtime dependency is added. The English managed block in `apps/web/AGENTS.md` preserves the installed Next.js generator's original text and requires consulting version-matched local documentation first.

## CI and merging

Every browser regression checks unhandled page errors through the shared fixture. Even when all tests pass, inspect CI logs for hidden hydration/runtime errors. See the [time-rendering regression and verification](time-hydration.md).

Workflows in `.github/workflows` define the required checks and commands. CI uses parallel language jobs, dependency caching, cancellation of superseded PR runs and timeouts. Missing, failed, cancelled or pending checks are not passing checks. Verify required checks and unresolved reviews for the exact latest head SHA before merging. Do not bypass protection or squash logical commits.

The current `CI` workflow requires `Python`, `Go` and `Web`. Python runs API/Runner tests, Ruff, PostgreSQL 17 upgrade/check/downgrade/re-upgrade, worker locking/isolation and [append-only audit](feedback-audit.md) tests. Go runs Worker/Gateway tests and vet. Web runs tests, type checking, ESLint, the production build and Chromium E2E. Browser binaries are cached; reports and failure evidence are retained in the `browser-evidence` artifact for seven days. Each job has an eight-minute timeout; superseded PR runs are cancelled. These short full checks currently need neither path-based skipping nor a multiple-version matrix.

GitHub Actions configuration follows the official [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) and [dependency caching reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching). Keep tokens read-only and pin external actions to verified SHAs.

Required check `Go` also runs `make test-monitoring`. It caches the official Prometheus 3.14.0 archive and verifies SHA-256 every time before testing configuration/rules. Synthetic series avoid real alert delays in CI. Existing required check names, eight-minute timeouts, and application coverage remain unchanged.

Required check `Python` also runs `python -m pytest -q apps/api/tests/test_cancellation_postgres.py` to verify cancellation/Claim races against actual PostgreSQL. Locally, set `KELPIE_TEST_POSTGRES_URL` to a dedicated test database URL. Each test creates a randomly named isolated schema and cleans up only that schema, without deleting existing data or audits. These four tests skip when the URL is absent; passing default SQLite tests alone does not complete concurrency verification.

Report each commit's purpose, automated and hands-on results, PR/CI/merge status, uncommitted changes, remaining MVP items and required external environments. Follow [AGENTS.md](../../AGENTS.md) for detailed repository rules.

[Delivery audit](delivery-audit.md)'s real Git/API/loopback-SCM regression runs in existing `make test-api` and required `Python` CI, without external SCM credentials or an extra service job. `test_audit_postgres.py` also verifies background constraints and retained-row migration. Future delivery changes must preserve approval provenance recording and revalidation immediately before external writes.

For [runtime observation](runtime-monitoring.md) changes, run `test_runtime_health.py` with `KELPIE_TEST_POSTGRES_URL` as well as SQLite. Required `Python` CI reuses its existing database service. Real HTTP failure/heartbeat/cancellation regression is in `make test-api`; alert timing, missing-data, and recovery semantics are in `make test-monitoring`. Do not confuse zero/missing/failed observations or replace actual HTTP verification with mocks for speed.
