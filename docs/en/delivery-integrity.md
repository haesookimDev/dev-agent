# Delivery patch byte integrity

[한국어](../ko/delivery-integrity.md) | English

## Protected boundary

A database SHA-256 matching approval evidence does not prove the stored file still exists or is unchanged. Downloads and central-delivery approvals through web/signed Slack now verify the actual file size and SHA-256. Delivery attempts, including startup recovery, verify against the approved hash again before token issuance. New Git work applies only a private copy of those verified bytes; replacing the original path later cannot change the delivered patch.

- Only nonempty regular files up to the existing 20MiB upload limit are accepted. Invalid hashes/sizes, changes during reading, missing files, directories/FIFOs, descendant symlinks, `..` and out-of-root paths are rejected.
- After opening operator-configured `ARTIFACT_ROOT`, each descendant is opened relative to its parent descriptor with `O_NOFOLLOW`, without checking then reopening the original path. An operator-controlled root alias is allowed, but descendant links are not. [Python file-descriptor API](https://docs.python.org/3/library/os.html#os.open)
- Verified in-memory bytes are exclusively written to `approved.patch` (0600), outside the checkout in the existing private temporary workspace (0700). Git reads only this copy; existing `finally` cleanup removes it. The changed original is not automatically repaired or overwritten.
- Approval identity/organization/repository/version/hash checks, worker quarantine, write locks and deadlines remain. Mock workers still bypass central delivery; this is not proof of real Git delivery.

This verifies the current local storage boundary against trusted database/approval evidence. It does not fully isolate against a malicious process with the same OS identity or root access. Hashes are neither encryption nor author signatures; existing remote branch/PR trees are not attested. External writes cannot be atomically rolled back with the database.

## API and event compatibility

The request shape is unchanged: `POST /api/work-items/{id}/approvals` with `{"kind":"pull_request","decision":"approve"}`. Invalid files return `409 {"detail":"delivery bundle is unavailable or invalid"}` without creating/changing approvals, work versions, events, audits or delivery jobs. Existing authorization, quarantine, state and configuration checks take precedence. Rejection and budget approvals retain their meaning.

`GET /api/work-items/{id}/delivery-bundle` returns only verified patches with 200. Missing database metadata retains 404; missing/corrupt/disallowed files return `410 {"detail":"delivery bundle is unavailable"}`. Other organizations still receive 404. Responses exclude original paths, bytes, actual hashes and OS errors.

Corruption after approval uses the existing failed-work/job path. `delivery.failed` events and audits record `stage=bundle` with `error_code=bundle_unavailable` or `bundle_integrity_failed`; the latter covers size/hash/concurrent-read changes. An audit with `authorization=denied` may retain the verified original approval reference/hash. `delivery.started` proves approval-metadata checks, not subsequent byte-verification success. `publication=not_started` means failure before external calls in this attempt, not absence of writes from previous attempts.

Web displays its existing translated generic approval error with HTTP 409. No API schema, Worker/Runner/Gateway contract, environment variable/default, dependency or migration is added. External event-code allowlists must accept the `bundle` stage and `bundle_integrity_failed`. [Safe error contract](delivery-failure-safety.md), [approval audit](delivery-audit.md)

## Restore and operations

1. As required by [PostgreSQL restore gates](postgres-restore.md), stop/isolate writers and external delivery, preserving matching database and file snapshots. Do not restart the API after restoring only its database.
2. Current `DeliveryBundle.object_path` retains its stored path. Provide regular files at that path under the same configured `ARTIFACT_ROOT`. Never bypass this with old-root files or descendant symlinks. Automatic path relocation, ordinary artifact hashing and full object-store recovery are outside this change.
3. For corruption before approval, restore exact bytes from a trusted backup, reverify, then obtain approval. Never alter database hashes/audits/job links or disable checks to make verification pass. Arbitrary job-state repair/automatic retry after post-approval failure is unsupported. Reconcile remote outcomes, then use a new verified work item and explicit approval.
4. Rolling back to vulnerable code reopens the gap. Stop delivery and ship a forward fix instead of resuming delivery on that version. Read/hash cost is bounded to 20MiB per request; concurrent large-request and actual storage-failure load testing remain separate operational work.

## Verification · 2026-09-06

Executable code and regressions: `2d10f73`.

- Twenty-one regressions failed before the fix. Afterward, 23 utility cases, 10 web/Slack rejection cases, 11 delivery/recovery/snapshot cases and one additional real HTTP/Git regression passed. Existing real-Git private-filename disclosure assertions remain intact.
- `make test` against a dedicated PostgreSQL 17 database: API 571 (no skips), Runner 6, Worker/Gateway and Web 52/TypeScript passed. `make lint`, production Web build and `make test-monitoring` with 10 rules passed. Default `make test-api` passed 526 and skipped 45 PostgreSQL-only cases.
- Real Uvicorn, fresh SQLite migrations, Git clone/apply/commit/push and loopback SCM verify corrupt download 410/approval 409, then approval after exact restoration. Replacing the original during token issuance still publishes approved content once, with temporary-copy cleanup. This is not live GitHub App/IdP/Slack/KVM acceptance.
- The same fixture ran API `:18490` and production Web build `:13490`. Orca browser KO/EN approval-button clicks rejected corruption. After restoration, English approval reached completed/100%/PR link/closed feedback; remote Git bytes, audit and token/PR counts were cross-checked. There was no horizontal overflow at 1035px. Computer Use visually checked the actual Korean rejection and English completion desktop screens. OS focus was unavailable, so native keyboard acceptance is not claimed. No UI code changed.

Existing required `Python` CI includes the new tests without another service, matrix or duplicate build. Final-head CI and merge SHA are recorded in the PR. Ordinary artifact physical recovery, retention/janitors, active VM cleanup and real KVM/network/input isolation remain; the full MVP is not marked complete.
