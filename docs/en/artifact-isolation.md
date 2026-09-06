# Work-scoped local artifact storage

[한국어](../ko/artifact-isolation.md) | English

## Protected boundary

Artifact metadata belonging to a work item does not prove ownership of its file. Registration, downloads and uploads now accept only the exact `<work-id>/artifacts/<file or nested path>` namespace. Other work, similar ID prefixes, the same work's `delivery.patch`, root files, empty components, `.`, `..`, backslashes and NUL are rejected. Existing organization/repository authorization, work-scoped leases and worker quarantine remain enforced.

- After opening operator-configured `ARTIFACT_ROOT`, descendants are traversed using parent file descriptors and `O_NOFOLLOW`. An operator-managed root alias is allowed; descendant symlinks are not. The original path is never checked and then reopened by its mutable string.
- Writes exclusively create a 0600 temporary file relative to the same parent descriptor, then atomically replace the destination. New descendant directories use 0700. Failure removes only that temporary file and preserves existing content. A final destination symlink is replaced itself, without writing its target. [Python descriptor/atomic replacement API](https://docs.python.org/3/library/os.html#os.replace)
- Reads accept only regular files and bound actual size to 10MiB. FIFOs, directories, links, missing files and changes to size/modification metadata during reading are rejected. Existing upload requirements for nonempty content, 10MiB and supported types remain.

This assumes a trusted operator root and API/database ownership. It does not fully isolate malicious processes running as the same OS identity or root. Ordinary artifact hashes, agreement with metadata `size_bytes`, encryption, immutable Slack retransmission files and physical object-store restoration are outside this change. The subsequent [content policy](artifact-content.md) covers MIME and executable-document rejection; filename presentation and HTTP caching remain separate follow-ups.

## API compatibility

Request/response schemas and URLs are unchanged. Runner already uses `/api/runs/{work-id}/artifacts/upload` and needs no key-generation change. No setting, default, dependency or database migration is added.

- `POST /api/runs/{work-id}/artifacts`: for example, `{"kind":"evidence","name":"test.txt","content_type":"text/plain","object_key":"<work-id>/artifacts/test.txt","size_bytes":12}` still returns 201. Even with a valid lease, another namespace returns `422 {"detail":"artifact key must belong to this work"}` without metadata/events. Existing schema-level 422 validation takes precedence for paths rejected by the schema itself.
- `POST /api/runs/{work-id}/artifacts/upload`: valid uploads return 201. Storage-path links/filesystem errors return `503 {"detail":"artifact storage is unavailable"}` without publishing metadata/events. Responses exclude real paths, OS errors and file contents.
- `GET /api/work-items/{work-id}/artifacts/{artifact-id}`: valid files return 200. Even retained metadata with an out-of-scope key, link or unreadable file returns `410 {"detail":"artifact content is unavailable"}`. Other organizations or metadata from another work still receive 404. Invalid retained rows are not automatically deleted or rewritten.

## Operations and restoration

Existing upload-generated keys remain compatible. Manually registered out-of-scope keys or descendant-link-dependent data become unavailable. Preserve matching database/files before rollout and review keys. An operator must verify rightful work ownership and provenance before restoring legitimate files into that work's real `artifacts` directory. Never copy another work's file or rename IDs just to pass validation. No automatic key-relocation tool is provided.

Keep [database restore gates](postgres-restore.md), isolating writers and external delivery. Restoring only the database is not success. Existing permissions are not rewritten in bulk and evidence is not automatically removed. Do not resume artifact serving on the vulnerable version after rollback; stop that serving path and recover with a forward fix.

## Verification · 2026-09-06

Executable code and regression commit: `f179313`.

- Nine cross-work reference/symlink regressions failed before the fix and passed afterward. Added 23 storage cases, 10 API isolation cases and one real HTTP regression.
- `make test` against a dedicated PostgreSQL 17 database: API 605 (no skips), Runner 6, Worker/Gateway and Web 52/TypeScript passed. `make lint`, production Web build and `make test-monitoring` with 10 rules also passed.
- Real Uvicorn, fresh SQLite migrations and scoped Worker registration/claim/upload exercise two organizations with synthetic OIDC sessions: valid reads, cross-work registration 422, retained invalid-key/link reads 410 and linked-directory upload 503. This is not live IdP login, VM, SCM or Slack acceptance.
- The same fixture ran API `:18500` and a Next.js standalone production build at `:13500`. Orca browser artifact-link clicks showed valid text and the fixed rejection response. KO/EN detail screens, organization-switched list isolation/not-found screens and fresh-request 404 were verified. There was no horizontal overflow at 1035px. Computer Use visually checked Korean detail and English not-found desktop screens. No native focus was available, so OS keyboard acceptance is not claimed. No UI code changed.
- Initial browser entry preceded Web readiness and failed; entry succeeded after readiness verification. A cached, directly opened 410 response caused a cross-origin refetch failure; a fresh request returned the expected 410. Cache/content-presentation protection remains a separate follow-up PR. The page console contained no additional messages.

Existing required `Python` CI runs these regressions without another service, matrix or duplicate build. Final-head CI and merge SHA are recorded in the PR. Ordinary artifact physical restoration, retention/janitors and real KVM/network/input isolation remain; the full MVP is incomplete.
