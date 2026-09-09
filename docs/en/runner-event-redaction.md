# Runner event credential redaction

[한국어](../ko/runner-event-redaction.md) | English

## Egress boundary and compatibility

Runner `ControlClient.event` redacts a copy of the message, source, event type and nested payload before building the HTTP request body. `transition` messages receive the same treatment. Caller objects, actual commands, the authentication `X-Kelpie-Lease` header, correlation IDs, status/version and approval policies remain unchanged. There are no API schema, database migration, environment variable, dependency or CI job changes.

- Replace this client's **complete plaintext lease token** in strings and JSON keys with `[REDACTED]`. Clients do not share token lists or read the entire environment.
- Normalize case, `_` and `-`, then recursively redact values for Token, Access/Refresh/ID/Session Token, Authorization/Proxy Authorization, Cookie/Set Cookie, API Key, Secret/Client Secret, Password/Passwd, Private Key and Lease Token/X-Kelpie-Lease/KELPIE_LEASE_TOKEN fields. Preserve `token_usage`, `secret_count`, normal numbers, booleans and nulls.
- Redact Codex display messages, exit stderr and verification command output before applying existing length limits. Regressions check tokens crossing truncation boundaries. Structured startup error values are also redacted.

For example, Runner message `Result: <current lease token>` is retained and retrieved as `Result: [REDACTED]`. Payload `{"access_token":"<synthetic value>","token_usage":17}` becomes `{"access_token":"[REDACTED]","token_usage":17}`. The authentication header must remain valid; an invalid lease's HTTP error is not converted into success.

## Verification — 2026-09-09

On pre-fix `e51062f`, 13 credential-field/event-egress regressions failed. Implementation `5ac234c` passed `make test-runner` (28 tests) and `make lint`. The actual API regression in `51c74a2` verifies:

- Runner Client connections to actual Uvicorn/SQLite with disposable OIDC organizations/sessions, individually issued Worker credentials and work leases. No external IdP, Codex account or real VM is used.
- Actual child processes read an owned synthetic lease file, produce long output and fail (1) or succeed (0). The Runner preserves exit codes and safe output while redacting the lease. The temporary file is `0600`; only the owned probe directory is removed.
- Synthetic credentials do not occur in actual event retrieval, SSE, database rows, post-shutdown SQLite bytes or API logs. Invalid leases return 401, other-organization reads return 404, denied events are not stored and correlation IDs remain intact.

Full `make test` with a dedicated PostgreSQL database passed API 990 tests (193.16 seconds, no skips), Runner 28, Worker/Gateway, Web 91 and type checking. The new Runner HTTP regression itself uses SQLite; being included in a PostgreSQL-enabled full run does not make that regression PostgreSQL-backed. Static checks and the production Web build used for hands-on verification also passed. Existing Python CI includes the new tests without changing jobs, eight-minute timeouts or validation strength.

## Hands-on verification

Ran implementation `5ac234c`'s Runner with the disposable API and standalone Web. Initial browser entry preceded Web readiness and encountered a connection error; only re-entry after readiness is acceptance evidence. After retrieving 12 stored events, added a new event and observed `실시간 검증 / Live check: [REDACTED]` without refreshing. Verified Korean/English switching, a 1155px screen and both languages at 390px (document width 375px), live connectivity and readability. An actual computer-use desktop capture also showed the Korean events and redaction markers. OS focus was unsupported; native keyboard/mouse input was not verified.

Captured requests comprised 30 HTTP 200 responses and one login redirect entry without a recorded status, with no external requests or console messages. Login cookie values were not displayed. App UI code/design was unchanged; secret-containing before screens and whole-desktop captures were not published. Orca public artifact permissions were neither changed nor retried.

Stopped owned browser/terminal tabs, API, Web and login helper and verified no listeners on ports 13200/18200/18201. Scanned the final API log/database before disposable fixture cleanup; existing user sessions and data were untouched.

## Limits and follow-up

This strengthens a specific Runner egress boundary; it is not a general secret scanner or protection against an adversarial VM. Unknown tokens in free text, encoded/fragmented/image secrets, other exceptions' earlier truncation/local tracebacks/crash dumps, artifact/delivery-bundle bytes, cloud-init and direct API writes bypassing the Runner are not comprehensively covered. Previously retained events are not rewritten. SEC-001, actual VM isolation and the full MVP remain incomplete.

Adjust false positives with regression tests for safe fields; do not disable all redaction or bypass authentication. No schema rollback is needed, but an older Runner reopens existing disclosure paths, so rollback requires an approved environment where evidence transmission through those paths is safely stopped. Suspected production credential exposure requires revocation/rotation and investigation of retained evidence under an approved incident-response procedure.
