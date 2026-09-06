# Ordinary artifact file backup and restore

[한국어](../ko/artifact-backup.md) | English

## Scope and recovery point

This offline OPS-001 control-host command binds ordinary files in local `ARTIFACT_ROOT` to a matching database dump. Use it with [database backup/restore](postgres-restore.md): `pg_dump` does not contain external files. API/database schemas, environment defaults, organization permissions and download policies are unchanged. Delivery bundles, VM disks, previews, external object stores, scheduled backups, PITR and janitors are not included.

- Run the deployed API version on an authorized control host. Secret management must safely supply `DATABASE_URL` for the intended database. The CLI needs schema inspection and Artifact SELECT privileges only. It never starts the API, migrates/writes the DB, stops/restarts services or switches their paths.
- Operators must stop/coordinate ingress and DB/file writers, then create the DB dump and file snapshot at the same recovery point. `--writers-stopped` acknowledges this coordination; it is not a stop command, lock or proof. Before/after metadata and dump-hash comparisons cannot detect every concurrent write.
- Restore the DB into a separate new database first. The file command compares the actual dump SHA-256 and connected Artifact metadata, but does not prove that the connected DB came from that dump or that all ACLs/audits are correct. Keep full DB verification and current-revocation reconciliation gates.
- Backups contain user bytes, names and work IDs. SHA-256 is neither encryption nor author authentication. Use authorized encrypted storage, restricted access and retention policies; never upload dumps, snapshots, manifests or logs to Git, PRs or CI artifacts. Keep the manifest hash in trusted inventory protected independently of the backup. [Python hash functions](https://docs.python.org/3/library/hashlib.html)

## Create and independently verify

The following assumes `backup_dir` and `control.dump` from the [database backup procedure](postgres-restore.md), with writer coordination complete. `ARTIFACT_ROOT` must identify the source files and `DATABASE_URL` the source DB at that recovery point. Select actual paths/databases through authorized operational configuration.

```sh
set -eu
umask 077
.venv/bin/python -m app.artifact_backup_admin create \
  --database-dump "$backup_dir/control.dump" \
  --output "$backup_dir/artifacts" --writers-stopped
```

Require exit code 0 and `verified: true`; link the returned `manifest_sha256` to trusted inventory. stdout contains counts, verification status and the manifest hash only. Failures return code 2 with a fixed error excluding private values. Do not log file names or database connection details.

Set `trusted_manifest_sha256` from inventory. Never derive the trusted value automatically from the manifest being verified. Read back snapshots copied into authorized storage using the same command below.

```sh
.venv/bin/python -m app.artifact_backup_admin verify \
  --database-dump "$backup_dir/control.dump" --backup "$backup_dir/artifacts" \
  --manifest-sha256 "$trusted_manifest_sha256"
```

## Restore to a new root

Complete DB restore/isolation gates first and configure `DATABASE_URL` with a restricted reader for the restored candidate DB. `restore_root` must be a **new, absent root** under an authorized existing parent. Do not choose a path inside source/backup roots or reuse an existing candidate. Do not connect the API to this root before every command below succeeds.

```sh
set -eu
umask 077
.venv/bin/python -m app.artifact_backup_admin restore \
  --database-dump "$backup_dir/control.dump" --backup "$backup_dir/artifacts" \
  --manifest-sha256 "$trusted_manifest_sha256" \
  --output "$restore_root" --writers-stopped
ARTIFACT_ROOT="$restore_root" .venv/bin/python -m app.artifact_backup_admin verify-restored \
  --database-dump "$backup_dir/control.dump" \
  --manifest-sha256 "$trusted_manifest_sha256"
```

File restoration retains original object keys without creating/changing DB rows. `.kelpie-artifact-restore.json` is the manifest copy published after copied files pass readback verification. **Its existence alone never authorizes resumption.** Require successful commands, `verify-restored`, full database/access/external-delivery/VM-liveness gates and operational approval before restarting one API. File hashes do not replace [servable MIME/content validation](artifact-content.md).

A failed new directory may retain partial files; nothing is automatically deleted. Even with a marker, subsequent DB comparison/sync failures or later file changes are possible. Quarantine the candidate, investigate and retry at another new path. Never weaken permission checks to overwrite an existing file, directory or symlink. Before cutover, keep the source and abandon a candidate if necessary. After new writes, follow [DB rollback gates](postgres-restore.md) instead of blindly returning to the source.

## Format and limits

- Version 1 `manifest.json` contains `version`, `database_sha256`, `artifacts`. Entries contain `artifact_id`, `work_item_id`, `object_key`, `kind`, `name`, `content_type`, `size_bytes`, `sha256`. Remaining DB fields such as creation times are retained by the database dump.
- Deduplicated bytes live at `blobs/<SHA-256>`; no archive paths are extracted. Duplicate artifact IDs, different DB metadata, inconsistent aliases, invalid JSON/duplicate fields, unknown versions/fields and missing/tampered files fail verification.
- Limits are 10,000 metadata entries, 10 MiB per file and a 16 MiB manifest. Oversized/invalid rows fail the whole operation instead of being silently omitted. Empty files and separate metadata aliases for one key are retained. Storage is uncompressed; provide separate capacity for source, backup and restored copies.
- Work-scoped paths, no-follow descendant/final-file opens, regular-file/size checks and read-change detection are enforced. Operators must control root/parent paths. This is not a sandbox protecting roots from malicious processes under the same OS account.
- New directories use 0700 and files 0600, with exclusive creation, file/directory synchronization and no-overwrite final manifest publication. The filesystem must support these operations; power-loss durability and remote-store atomicity are not guaranteed. Reverify after failures.

## Regression, CI and actual use

`make test-api` includes 50 file-boundary and 11 CLI tests. The eight real restore tests below also run when `KELPIE_TEST_POSTGRES_URL` and `KELPIE_TEST_POSTGRES_CONTAINER` point to the same dedicated test server. Missing settings skip PostgreSQL coverage and are not success evidence.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_postgres_restore.py \
  apps/api/tests/test_postgres_restore_runtime.py apps/api/tests/test_artifact_restore_runtime.py
```

Required `Python` CI adds two file-restore tests to its existing PostgreSQL 17 service/restore step. No new jobs, dependencies, secrets or matrix; the eight-minute limit stays. Tests clean up only UUID databases/reader roles they created, compare full source/restored DB fingerprints and retain source files unchanged.

On 2026-09-06, app `e6f36a5` and regression `68e6d15` passed eight real PostgreSQL restore tests in 9.88s, dedicated-PostgreSQL `make test` (API 801/98.86s, Runner 6, Web 83/type checking, Worker/Gateway), and `make lint`. Actual Uvicorn/Next.js browser checks with synthetic OIDC sessions and a SELECT-only DB login covered the 410 notice → API stop/CLI restore/restart → same-URL text 200/retry focus, plus PNG rendering at Korean 390px width. HTTP checks also cover foreign-organization 404, audit 403 and unauthenticated 401. These are not actual IdP, operational recovery or VM verification. Final head, browser evidence, CI and merge results are recorded in the PR.
