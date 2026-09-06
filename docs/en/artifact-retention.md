# Ordinary artifact retention

[한국어](../ko/artifact-retention.md) | English

## Scope and prerequisites

Only ordinary local files under the control host's `ARTIFACT_ROOT/<work UUID>/artifacts/...` are selected. VM disks, Delivery Bundles, Events, Preview, Console, audit records, external object stores and older backups are not deleted. This does not prove physical VM termination/isolation or complete OPS-001.

This is a bounded control-host CLI for an operator-approved retention policy and storage root. It adds no timer, cleanup API endpoint or default retention period. It uses existing `DATABASE_URL` and `ARTIFACT_ROOT`; there are no new environment variables or dependencies. It unlinks files: do not apply it to production without an approved retention policy.

1. Establish a coordinated DB/file recovery point using the [backup procedure](artifact-backup.md).
2. Apply migration `20260906_0010` and **upgrade every API instance and the retention/backup CLIs together**. Do not start retention while old API readers that cannot interpret expiration intent are running.
3. Confirm the exact storage root is mounted and writers follow the control-plane lease-validation boundary. This is not a sandbox against malicious file replacement by the same OS account.
4. Start with a dry run for a verified `--work-id`. The following UUID is an example, not a production target.

```sh
python -m app.artifact_retention_admin --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100

# Apply only the same reviewed scope
python -m app.artifact_retention_admin --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --apply
```

`--retain-days` is a required integer from `1..36500`. Omitting `--apply` leaves the DB, audit and files unchanged. Omitting `--work-id` scans **all works**: confirm authorization before widening the scope. `--limit` bounds metadata candidates per invocation, default 100 and maximum 1000. When `next_cursor` is present, use `--after-artifact-id <next_cursor>` with the same policy/work scope until it becomes `null`. Start a new sweep without a cursor. Repeatedly scanning only the first page can miss later candidates behind protected entries.

Output contains only `dry_run`, `scanned`, `counts`, fixed `reasons` and `next_cursor`. `eligible` identifies a dry-run candidate; `protected` means preservation by a safety guard; `purged` records completed deletion; `already_purged` means another invocation completed it. Multiple metadata aliases for one file are handled together, so `purged_aliases` can exceed `scanned`. `bytes_removed` counts bytes unlinked in this invocation only. Failures exit 2 without undoing earlier successes. Paths, names and raw database diagnostics are withheld.

## Protection and two-phase processing

Each transaction separately locks and rechecks these conditions:

- Work is `completed` or `cancelled`, with its last update older than the policy. Retryable `failed` work is excluded.
- Its Worker is not quarantined and the consistently owned lease is explicitly `released`. A time-expired `active` lease is still protected. A never-assigned final work must have neither a Worker nor a lease.
- DeliveryJob is absent or `completed`, with no unexpired Preview/Console lease.
- Every alias of the object key belongs to the same work, agrees on size/expiration state and is old enough. At most 10,000 aliases are processed. Cross-work aliases and recent artifacts are protected.
- UUID, work-scoped path, regular-file type, 10 MiB maximum, size and SHA-256 are verified. Descendant symlinks, special files, changed content and unreachable root/parent directories fail closed. No recursive deletion is used.

Transaction one commits `expired_at`, `retention_days`, `retention_sha256` and an `artifact.expiration_requested` audit together. New downloads are denied from then on. Transaction two reacquires the guards and checks identical intent, unlinks only the exact file, fsyncs its directory, then commits `purged_at` and `artifact.purged`. Metadata including filename/size and existing audits remain intact. Service audits use subject `artifact:retention`, provider `urn:kelpie:service`, transport `background`, organization/work/request IDs, policy and content digest.

After an intermediate failure, **retry the same retention period and scope**. Completion after an already-unlinked file requires its parent directories to remain reachable. A post-unlink DB failure neither restores bytes nor duplicates audit entries on retry. New quarantine, delivery or alias changes defer deletion for investigation. Do not change the policy or clear expiration fields to bypass a deferral.

PostgreSQL locks Worker → ResourceLease → Work → DeliveryJob → Preview/Console → Artifact, with a 2-second lock timeout and 15-second statement timeout. SQLite serializes writers using `BEGIN IMMEDIATE`. Bounded synchronous file IO is confined to the CLI so cancellation cannot release DB locks while an unjoined thread continues deleting. Slow storage can still delay a pass; begin with small batches.

## API, UI and recovery compatibility

The existing list response gains only `expired_at: null | timestamp`; storage keys, hashes and retention policy remain private. After authorization, expired downloads return HTTP 410 with `{"detail":"artifact retention period has expired"}` and retain `Cache-Control: no-store`. Other organizations/works still receive 404, unauthenticated clients 401. Previously downloaded copies cannot be remotely erased.

Both locales retain an expiration badge, metadata and explanation without file-opening controls. A stale list also learns expiration from a 410 without suggesting recovery/re-upload. Closing a modal restores keyboard focus to the evidence heading if its original button disappeared. No dedicated expiration SSE event or periodic UI refresh is added; subsequent reads/opens discover the state.

[Backup V2](artifact-backup.md) retains completed expiration evidence without restoring its bytes, and rejects pending deletion intent. V1 is compatible only when the matching DB has no expiration evidence. A pre-expiration DB/file backup cannot know later expiration, so reconcile current expiration/access records before republishing it.

For rollback, stop retention and use a forward fix that preserves the expiration read gate, revision 0010 and audits. The 0010 downgrade refuses to discard expiration evidence. Clearing expiration fields, simply deploying the old API, overwriting an existing root or blindly restoring old files is not a safe rollback.

## Verification evidence

The verified implementation is `923425c`; the following documentation/evidence updates do not change executable code. Verification used isolated macOS local resources, PostgreSQL 17, Chromium and an Orca window.

- `make test` with real PostgreSQL passed 983 API, 6 Runner, 91 Web cases and Worker/Gateway Go tests; `make lint` also passed.
- Verified 26 file-safety, 52 lifecycle/failure/retry, 19 real-CLI/input-boundary, 2 authorized-API and 16 real PostgreSQL retention/lease/quarantine concurrency cases.
- Actual PostgreSQL dump/clean DB restore and SELECT-only file backup CLI verified expiration/audit/ACL preservation, live-file recovery and absence of expired bytes. Only UUID-owned databases/schemas/files were used.
- `make test-web` passed 91 cases, Web lint/production build passed, and Chromium passed 29 cases. The first full run observed one development-mode `Performance.measure` error on an existing 404 page. Without changing error checks, three targeted repeats and the full 29-case rerun passed. This is not claimed as a root-cause fix; CI retains the original error checks.
- In a real macOS Orca window: open before retention → run CLI → reopen without refreshing to see expiration → Escape → heading focus restoration → open a live artifact → switch locales. At 1280px and 390px there was no horizontal overflow; badge/explanation contrast measured 6.34/5.70. Computer-use verified native-window rendering, but unsupported OS focus means this is not evidence of OS input or real VM behavior.
- Only 35 synthetic artifact bytes were removed; recent and other-organization files remained. Test cookies/tabs/dedicated services and temporary DBs were cleaned up. Production data was not changed.

[Before retention](../assets/artifact-retention/before.png) · [Korean expired list](../assets/artifact-retention/after-ko.png) · [English list](../assets/artifact-retention/after-en.png) · [Mobile](../assets/artifact-retention/mobile-ko.png) · [Reopen notice](../assets/artifact-retention/expired-dialog-ko.png)
