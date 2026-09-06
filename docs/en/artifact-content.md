# Artifact content and browser execution boundary

English | [한국어](../ko/artifact-content.md)

## Protected boundary

Artifacts must not become executable documents at the API origin. Metadata registration and uploads accept only the existing upload formats: `image/png`, `image/jpeg`, `image/webp`, `text/plain`, and `application/json`. HTML, SVG, XHTML, PDF, JavaScript and arbitrary binary types are unsupported. File extensions are not trusted and Content-Type is not guessed.

Downloads repeat the same content validation after the [work-scoped storage boundary](artifact-isolation.md) reads at most 10MiB. Image checks retain existing PNG/JPEG/WebP signature checks, not full image decoding or malware scanning. Text must decode as UTF-8 and JSON must parse; parser recursion errors also produce fixed rejection responses. No separate JSON depth limit or new schema is introduced.

Successful responses retain `X-Content-Type-Options: nosniff` and add `Content-Security-Policy: sandbox`. Do not add `allow-scripts` or `allow-same-origin`. Sandbox restricts document scripts and isolates the document to an opaque origin. [MDN sandbox policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox), [MIME-sniffing protection](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Content-Type-Options)

Existing organization/repository authorization, work-scoped leases, worker quarantine and path checks remain. Ordinary artifact hashes, metadata `size_bytes` agreement, physical backup/restore, filename encoding and HTTP caching are outside this change.

## API compatibility and operations

URLs and request/response fields remain unchanged. No setting, default, dependency or database migration is added.

- `POST /api/runs/{work-id}/artifacts`: `{"kind":"evidence","name":"report.txt","content_type":"text/plain","object_key":"<work-id>/artifacts/report.txt","size_bytes":12}` still returns 201 with a valid lease/path. Changing `content_type` to `text/html` returns `415 {"detail":"unsupported artifact type"}` without metadata/events. Actual bytes are checked on download.
- `POST /api/runs/{work-id}/artifacts/upload?name=report.txt&content_type=text/plain`: valid UTF-8 returns 201; unsupported formats retain 415; mismatching bytes return `422 {"detail":"artifact content does not match its declared type"}`.
- `GET /api/work-items/{work-id}/artifacts/{artifact-id}`: valid data returns 200 with the security headers above. Retained unsupported MIME or changed file formats return `410 {"detail":"artifact content is unavailable"}`. Work from another organization still returns 404. Invalid rows are not automatically rewritten or deleted.

Previously registered HTML and similar links become unavailable, which affects compatibility. Preserve corresponding database/files and review formats before rollout. If needed, verify ownership/provenance and separately produce safe image/text/JSON evidence. Do not merely retag MIME to bypass validation or bulk-overwrite historical rows. On rollback, stop the affected serving path and recover with a forward fix instead of resuming artifact serving on the vulnerable version.

## Verification · 2026-09-06

Executable code and regression commit: `a2e18e5`.

- Before the fix, 22 of 23 regression cases failed. A harmless DOM-message probe also reproduced script execution at the actual API origin. It reads no credentials and makes no external requests.
- Added 24 API content cases and one real Uvicorn HTTP case. They cover rejected registration, retained MIME, all five valid formats, post-upload tampering, lease/organization boundaries, parser failure and security headers.
- `make test` against a dedicated PostgreSQL 17 database passed: 630 API tests with no skips, 6 Runner tests, Worker/Gateway, and 52 Web tests plus TypeScript. `make lint`, production Web build, 10 monitoring rules and 19 Chromium E2E cases passed. The new browser case took about two seconds; the full E2E suite about 1.5 minutes.
- The Chromium case uses actual temporary SQLite, synthetic OIDC sessions and scoped Worker registration/claim/upload. It verifies text is not parsed as markup, opaque origins, a genuinely loaded 32×32 PNG, readable JSON and 410 for retained HTML. Shared unhandled-page-error and executed-work release checks remain. Only this case omits traces containing temporary authentication headers; failure screenshots/reports remain enabled.
- Using the same fixture's Uvicorn `:18510` and production Next.js standalone `:13510`, opened artifact links in Orca. Verified valid text/PNG/JSON, unsupported HTML rejection, Korean/English detail views, no horizontal overflow at 1035px and fresh 200/410 responses. The console contained no additional messages. Computer Use verified both dashboard languages, PNG, and text/rejection responses in light mode. This is not real IdP login, VM, SCM or successful OS-input verification.

Orca's dark-mode native text/JSON documents displayed white text on a white background. Light-mode comparison showed black, readable content. This is distinct from captures where the target tab was not revealed; native coordinate input was rejected by the focus guard and not retried. Artifact-viewer contrast/error guidance, filename encoding and the existing cached-410 issue remain separate follow-up PRs. No product UI code changes in this PR.

New API/browser regressions run in existing required `Python`/`Web` CI, without additional jobs, matrices or dependencies. Final head CI/merge SHA are recorded in the PR. Physical artifact recovery, retention/janitors and actual KVM/network/input isolation remain, so the overall MVP is incomplete.
