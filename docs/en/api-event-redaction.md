# API event ingress credential redaction

[한국어](../ko/api-event-redaction.md) | English

## Ingress boundary and compatibility

Relying only on [Runner egress redaction](runner-event-redaction.md) lets other Workers or direct HTTP clients retain raw values. After validating the lease, the API redacts copies of event type/source/message/nested payload on `POST /api/runs/{id}/events`, and message/payload on `POST /api/runs/{id}/transition`, before storage.

- The **complete plaintext lease token** authenticated on the current request becomes `[REDACTED]` in strings and JSON keys. No other work's token list or entire environment is read.
- Normalize case, `_` and `-` to recursively redact the [same 18 credential fields as the Runner](runner-event-redaction.md). Preserve `token_usage`, `secret_count`, normal numbers, booleans and nulls. Contract tests check policy parity without adding a Runner runtime dependency to the API.
- Lease validation, organization/repository authorization, status/version/approval policies and correlation IDs remain intact. No API schema, database migration, environment variable, dependency or CI job changes. Existing clients need no changes, but retained credential values intentionally change.

Example request with a valid `X-Kelpie-Lease` (displayed values are placeholders, not credentials):

```json
{"event_type":"worker.output","message":"Result: <current lease>","payload":{"access_token":"<synthetic value>","token_usage":42}}
```

The 200 response and subsequent event log/SSE contain message `Result: [REDACTED]` and payload `{"access_token":"[REDACTED]","token_usage":42}`. Missing/invalid/other-work/expired/quarantined leases return 401; stale versions/forbidden transitions return 409 without changing state or events.

## Verification — 2026-09-09

On pre-fix `4c5534e`, two direct-event-storage/transition regressions failed. Implementation `6bcf140` passed 33 API specification/contract tests, 28 Runner tests and static checks. Independent verification `90cad52` sends raw values to actual Uvicorn/SQLite without using Runner redaction functions.

- Authenticate with disposable OIDC organizations/sessions, individually issued Worker credentials and leases; check event/transition responses, stored rows and retrieval. Open SSE before sending a new event to verify live delivery.
- Reject six writes with missing, forged or other-work leases and compare the full work state/event list before and after denial. Other-organization retrieval/SSE returns 404.
- Scan final API logs and post-shutdown SQLite bytes for synthetic plaintext credentials. An initial test incorrectly compared POST/GET timestamp representations; it now compares full state across the same retrieval boundary. Status/version/correlation ID checks and denial invariance remain intact.

Final `make test` with a dedicated PostgreSQL database passed API 1,024 tests (199.81 seconds, no skips), Runner 28, Worker/Gateway, Web 91 and type checking. The new actual HTTP regression itself uses SQLite. Focused regressions (34 new API tests plus 28 Runner tests), `make lint`, the production Web build used for hands-on verification and 108 relative documentation links also passed. Existing Python CI includes these tests without changing jobs, eight-minute timeouts or validation strength. The unchanged Web's full local E2E suite was not rerun; the PR's existing Web CI checks it.

## Hands-on verification

Ran disposable API and production standalone Web with implementation `6bcf140` and verification `90cad52`'s behavior. Retrieved six stored events, then sent a seventh by direct HTTP and observed `브라우저 직접 전송 / Browser direct HTTP: [REDACTED]` without refreshing. Verified Korean/English, a 1155px screen and both languages at 390px (document width 375px), live connectivity and readability. Computer-use confirmed the redaction markers on the actual desktop. OS focus was unsupported; native keyboard/mouse input was not verified.

All ten requests recorded after capture started returned HTTP 200, with no external requests or console messages. The login cookie was HttpOnly and its value was not displayed. UI code/design was unchanged; secret-containing before screens and whole-desktop captures were not published. Orca public artifact permissions were neither changed nor retried.

Stopped owned browser/terminal tabs, API, Web and login helper and verified no listeners on ports 13300/18300/18301. Removed only owned disposable fixtures after final log/database scans. No external IdP, SCM or real VM was used.

## Limits and rollback

Only these two accepted, authenticated telemetry paths are covered. Validation-stage 422 responses, other APIs, unknown free-text secrets, encoded/fragmented/image secrets, artifact/delivery-bundle bytes and comprehensive local-log/crash-dump/cloud-init verification remain outside scope. This is not a general secret scanner, historical event cleanup or an adversarial-VM exfiltration defense. Authenticity of event provenance and integrity of payload state metadata are also separate from this redaction verification. SEC-001 and the full MVP remain incomplete.

No schema rollback is needed, but an older API reopens direct-transmission disclosure paths. Roll back only in an approved environment after safely stopping affected evidence transmission; suspected actual exposure requires credential revocation/rotation and approved incident response. Adjust false positives with regression tests, not by disabling all authentication or redaction.
