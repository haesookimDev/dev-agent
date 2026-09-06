# Artifact filenames and HTTP headers

English | [한국어](../ko/artifact-filenames.md)

## Behavior and compatibility

Fixes 500 responses when downloading uploaded Korean/emoji filenames. Instead of inserting the original name directly into a Latin-1 header, preserve it as UTF-8 percent encoding in the ASCII `filename*` parameter. The legacy `filename` retains valid ASCII names without `%`; otherwise it uses `artifact`, avoiding percent reinterpretation by some clients. [RFC 6266](https://www.rfc-editor.org/rfc/rfc6266.html), [RFC 8187](https://www.rfc-editor.org/rfc/rfc8187.html)

- Registration and upload accept names of 1–255 code points. Empty names, leading/trailing whitespace, `.`/`..`, `/`, `\`, `"`, Unicode `Cc` controls and `Cs` surrogates are rejected. Internal spaces, Korean, accents, emoji, ZWJ and `%` remain valid.
- Rejected new names return 422 without rows, events or files. Previously accepted whitespace/control names now fail, so producers must send a valid basename. Existing Runner uploads and Web artifact URL contracts remain.
- Invalid historical names are not rewritten or deleted in the database. Only files passing ownership, path and content checks are served with suggested name `artifact`. Invalid MIME/path still returns 410, foreign organizations still receive 404, and lease checks remain.
- This does not guarantee Unicode normalization, every OS's reserved-name rules, extension/MIME agreement or malware detection. Not all `Cf` characters are forbidden. Browser/OS policy also affects names saved on a user's device.

## API example and operations

With a valid lease, `POST /api/runs/{work-id}/artifacts/upload` using a UTF-8 text body and URL-encoded query `name=검증 결과 ✅.txt&content_type=text/plain` returns 201. Metadata registration applies the same name policy. `GET /api/work-items/{work-id}/artifacts/{artifact-id}` returns original bytes and these headers:

```http
HTTP/1.1 200 OK
Content-Disposition: inline; filename="artifact"; filename*=UTF-8''%EA%B2%80%EC%A6%9D%20%EA%B2%B0%EA%B3%BC%20%E2%9C%85.txt
X-Content-Type-Options: nosniff
Content-Security-Policy: sandbox
```

`100%20 complete; v2.txt` becomes `filename*=UTF-8''100%2520%20complete%3B%20v2.txt`, preserving literal `%20` instead of turning it into a space. Forbidden names within schema length bounds return `422 {"detail":"invalid artifact name"}`. Existing schema failures such as length violations keep their validation responses. Valid files with invalid retained names use `inline; filename="artifact"; filename*=UTF-8''artifact`.

No settings, defaults, dependencies or database migrations are added. No bulk data rewrite is needed. Review registration producers' names and update the API on rollout. For rollback, stop the affected serving path and recover with a forward fix rather than resuming vulnerable header serving. Existing [content protection](artifact-content.md) and [work-scoped storage boundaries](artifact-isolation.md) remain intact.

## Verification · 2026-09-06

Executable code and regression commit: `77a77fc`.

- Before the fix, 37 of 40 API cases failed and three passed. Actual Uvicorn also reproduced a download 500 after a valid Korean upload. Added and passed 43 filename cases and one real HTTP case. Only harmless header probes are used, without credentials or external communication.
- `make test` against a dedicated PostgreSQL 17 database passed: 674 API tests with no skips in about 85 seconds, 6 Runner tests, Worker/Gateway, and 52 Web tests plus TypeScript. `make lint`, production Web build and 10 monitoring rules passed.
- All 19 Chromium E2E cases passed in about 91 seconds. The existing content case now exercises real Alt-click link downloads, asserting Korean/emoji and percent filenames, successful completion and original-byte equality. [Chrome download shortcut](https://support.google.com/chrome/answer/157179?hl=en), [Playwright browser-decided filenames](https://playwright.dev/docs/api/class-download#download-suggested-filename)
- Used real temporary SQLite, scoped synthetic OIDC sessions and Worker registration/claim, with Uvicorn `:18520` and production Next.js standalone `:13520`. Actual registration rejected an invalid name with 422; Korean/percent/invalid-retained-name downloads returned 200 with safe headers and unchanged bytes.
- Opened artifact links from Korean/English detail views in Orca. All three files remained literal text with opaque origins and no scripts. The dashboard had no horizontal overflow at 1035px, no console messages and no errors in six captured requests. Computer Use visually verified both dashboard languages and Korean-file content in light mode. This is not verification of native OS save dialogs, real IdP login, VMs or SCM.

New tests run in existing required `Python`/`Web` CI without more jobs, matrices, timeouts or dependencies. Final head CI and merge SHA are recorded in the PR. There are no product UI code changes requiring before/after UI attachments; desktop captures showing other projects are not published.

HTTP caching and viewer dark-mode contrast/error guidance remain separate follow-up PRs. Physical file recovery, retention/janitors and actual KVM/network/input isolation also remain, so the overall MVP is incomplete.
