# Private Worker failure diagnostics

[한국어](../ko/worker-private-diagnostics.md) | English

## Boundaries and compatibility

Failed HTTP responses and command output may contain credentials, leases, URLs or paths. [API event redaction](api-event-redaction.md) cannot protect Worker-local logs or know every other credential. The Worker discards raw errors at these boundaries, retaining only fixed classifications and numeric codes:

- HTTP JSON encoding, request construction, transport, response/Claim decoding. Error bodies and server-supplied Status text are excluded; HTTP codes and standard status names remain.
- VM preparation file access/writes and assignment encoding. Configured paths are excluded from error strings.
- Command stdout/stderr, on both success and failure, go to the null device without buffering. Start failure, exit code, Context cancellation and deadline expiry remain distinguishable.
- Logs and `worker.failed` events receiving executor errors. Only known safe errors retain their classification; others become `worker execution failed`. Startup logs omit the free-form work title but retain work/correlation IDs.

Examples: `control plane returned HTTP 403 Forbidden`, `control plane returned HTTP 409 Conflict`, `VM base image unavailable`, `VM command failed (exit 7)`.

The event Type/Source/Level/Payload contract remains unchanged; only Message is restricted. No API schema, migration, environment variable or dependency changes. Approval, version checks, authentication, credential reloading and per-work leases remain intact. Failure handling still reads current state→performs the required failed transition→releases with acknowledgement. Failed release retains the local reservation.

## Repeatable verification

```sh
make test-worker
(cd apps/worker && go test -race ./...)
make lint
```

- `client_privacy_test.go`: HTTP 401/403/409/422/500/503, arbitrary Status/body, encoding/transport/response failures, real TCP lease isolation and cancellation.
- `execution_privacy_test.go`: both streams from real shell processes, exit/start failures, arbitrary errors/titles, failure events, reservation retention and wrapped safe errors.
- `cmd/kelpie-worker/privacy_test.go`: builds and runs the actual Worker binary through rejected registration, missing image and command failure. Only its HTTP server and failing `qemu-img` are synthetic; it creates no VM. It verifies header isolation, correlation, transition version, one release, private-free logs/events and SIGTERM shutdown.

Four HTTP-boundary regressions and three execution-boundary regressions failed before their fixes. The commands above passed for implementations `5c2b259`/`5a506c1` and process regression `66a78ec`. Full `make test` with an owned PostgreSQL database passed API 1,270 (237.51s), Runner 45, Worker/Gateway, Web 125 and type checks. Only one Linux service syntax check was skipped on macOS; Linux CI runs it. Existing Go CI includes the new tests automatically without extra jobs, matrices or longer 8-minute timeouts.

## Actual use and limitations

On 2026-09-09, an isolated Uvicorn/SQLite API, individual Worker credential and actual binary exercised command failure and missing image separately. Both works reached `failed`, version 3, `released` lease and zero active runs; the database and API/Worker logs did not contain that plaintext Worker credential. The command was an owned failure fixture, not KVM acceptance.

With the queued page open in Orca, command failure and release history appeared without reloading. Korean→English navigation and closed feedback were verified. An initial temporary-script column-name mistake and duplicate execution input were corrected without product changes, then verified in a fresh disposable environment.

The development server rejected capture-time CSS requests lacking an origin, and capture calls failed with an Orca connection closure. Native inspection exposed only an accessibility container with window focus unsupported. These are not claimed as successful visual/native-input verification; allowlists and OS permissions were not bypassed. UI code/layout did not change.

On 2026-09-10, a production build/standalone Web reconfirmed missing-image rendering in Orca. Orca Screenshot failed with a window-focus timeout, but a separate disposable Chromium verified both errors at Korean 390px/English 1280px. All four captures were visually reviewed, with no horizontal overflow, page/console errors or HTTP errors. Orca's latest 25 requests were also 200; historical 404s were SSE reconnects for the two already-removed earlier fixtures. Chromium results do not substitute for unverified native input.

This is not a general secret scanner. Historical log cleanup, every successful-event metadata field, encoded secrets, artifact/crash-dump/cloud-init files, VM/child-process-owned logs and every error path in other components remain separate. This does not complete SEC-001, real KVM acceptance or the entire MVP.

## Operations and rollback

Use work/correlation IDs, HTTP codes, command exit codes and safe classifications for diagnosis. Further investigation requires reproducing the relevant step in an approved isolated environment without copying private raw output into shared logs. Do not add an automatic raw-response debug collector.

No schema rollback is required, but an older Worker reintroduces exposure. Stop that Worker's execution through an approved procedure before rollback; suspected exposure requires credential revocation/rotation and incident response. Do not disable authentication, redaction or approval boundaries.
