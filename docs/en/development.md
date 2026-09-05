# Development and verification

English | [한국어](../ko/development.md)

## Definition of done

Develop on a task branch and commit each logical unit with a Korean message after its relevant tests pass. Once automated tests and hands-on verification pass, create a PR with evidence and inspect CI and reviews for the latest commit. When the user delegates merging, the agent merges with a merge commit and fast-forwards local `main`. Keep a PR in Draft when required verification cannot run.

Continuous MVP development uses the roadmap's [next release](roadmap-summary.md#immediate-next-release) as its acceptance boundary. Do not automatically include P3 and later expansion work. Merge delegation for the current MVP does not authorize production deployment, paid infrastructure, bypassing branch protection, or removing the product's user-approval gates.

## Verification

- `make test`: API, Runner, Worker, Gateway, Web tests and Web type checking
- `make lint`: Python Ruff, Go vet and Web ESLint
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
npm run test:e2e:preview --prefix apps/web
```

Playwright runs actual Chromium, Next.js, API and Mock Worker processes. Each run migrates a temporary SQLite database, uses a random test Worker credential and cleans up only its own processes and data. Keep ports `13100` (Web) and `18100` (API) free. Stop any `next dev` instance in the same checkout first. Set `KELPIE_E2E_PYTHON` only to override `.venv/bin/python`. On Linux, use `playwright install --with-deps chromium` to install system libraries too.

Coverage includes creation, live events, feedback, re-verification, approval and resource release; search, filters, language and narrow layouts; network/permission failure retries; stream recovery; and invalid work addresses. The test Worker has one execution slot; every test uses the automatic resource-release fixture from `e2e/fixtures`. Failures preserve screenshots and traces in `apps/web/test-results`. The HTML report in `apps/web/playwright-report` is generated and must not be committed. A production build after E2E also regenerates the development-updated `next-env.d.ts` for production.

Playwright is a development dependency for real browser/service contracts that unit tests cannot exercise. No runtime dependency is added. The English managed block in `apps/web/AGENTS.md` preserves the installed Next.js generator's original text and requires consulting version-matched local documentation first.

The separate [OIDC Preview journey](preview-access.md) generates short-lived certificates and a synthetic IdP to verify TLS, one-time exchange, real WebSockets and revocation. It uses distinct ports and a temporary database, running sequentially after the existing browser suite. Traces that could contain OIDC codes/cookies are not retained. It does not replace network/VM isolation verification.

## CI and merging

Every browser regression checks unhandled page errors through the shared fixture. Even when all tests pass, inspect CI logs for hidden hydration/runtime errors. See the [time-rendering regression and verification](time-hydration.md).

Workflows in `.github/workflows` define the required checks and commands. CI uses parallel language jobs, dependency caching, cancellation of superseded PR runs and timeouts. Missing, failed, cancelled or pending checks are not passing checks. Verify required checks and unresolved reviews for the exact latest head SHA before merging. Do not bypass protection or squash logical commits.

The current `CI` workflow requires `Python`, `Go` and `Web`. Python runs API/Runner tests, Ruff, PostgreSQL 17 upgrade/check/downgrade/re-upgrade, worker locking/isolation and [append-only audit](feedback-audit.md) tests. Go runs Worker/Gateway tests and vet. Web runs tests, type checking, ESLint, the production build and Chromium E2E. Browser binaries are cached; reports and failure evidence are retained in the `browser-evidence` artifact for seven days. Each job has an eight-minute timeout; superseded PR runs are cancelled. These short full checks currently need neither path-based skipping nor a multiple-version matrix.

GitHub Actions configuration follows the official [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) and [dependency caching reference](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching). Keep tokens read-only and pin external actions to verified SHAs.

Report each commit's purpose, automated and hands-on results, PR/CI/merge status, uncommitted changes, remaining MVP items and required external environments. Follow [AGENTS.md](../../AGENTS.md) for detailed repository rules.
