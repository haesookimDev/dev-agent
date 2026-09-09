# Restartable scheduled ordinary-artifact retention

[한국어](../ko/scheduled-retention.md) | English

## Scope and prerequisites

This implements only OPS-001's **ordinary local artifacts**. It reuses every [existing retention](artifact-retention.md) guard for active leases, Worker quarantine, terminal work, Preview/Console, aliases, file integrity and two-phase auditing. It never starts automatically inside the API or a VM. No HTTP API, default retention period or dependency is added. Events, Delivery Bundles, VM disks, audits, external object stores and older backups remain outside this cleanup.

First verify the approved policy, exact `DATABASE_URL`/`ARTIFACT_ROOT` and a [coordinated DB/file recovery point](artifact-backup.md). Upgrade all API and backup/retention tools together and run `alembic upgrade head` through `20260909_0011`. Every batch checks database readiness against the deployed code. This document does not authorize production deployment or deletion.

```sh
# Example 30-day policy and UUID, not a production target
python -m app.artifact_retention_worker --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --once

# Apply only the reviewed scope; omit --once to wait 300 seconds after each batch and repeat
python -m app.artifact_retention_worker --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --mode apply --once
```

Default `--mode dry-run` **writes scan progress only**, leaving artifacts, audits and files unchanged. For a completely read-only database operation, use the existing `artifact_retention_admin` dry run instead. Required `--retain-days` is an integer 1..36500; `--limit` is 1..1000 (default 100); `--interval-seconds` is 1..86400 (default 300). Omitting `--work-id` selects **all works** and requires approval for that wider scope.

## Progress and failure handling

`artifact_retention_jobs` stores only a hash of canonical root/policy/work scope/dry-run-or-apply, cursor, version, update time and sweep completion time. It stores no path, database URL, credential or file content. Changing batch size or delay retains progress; changing policy, root, work or mode uses separate progress. Dry-run progress cannot skip apply candidates.

A successful page saves its next cursor, visiting candidates beyond protected early entries. Reaching the end clears the cursor so the next sweep revisits new artifacts and previously protected work. The existing per-file expiration intent/completion journal makes deletion retryable without duplicate audits even if saving progress fails. Operate one instance per scope. Version-conditional writes prevent concurrent stale checkpoints from replacing newer progress; this is not distributed leader election that eliminates duplicate scans.

Each completed batch emits one JSON line. For example, a first page protecting an active lease has this shape (hash and UUID are illustrative):

```json
{"job_id":"<scope SHA-256>","checkpoint_saved":true,"checkpoint_version":2,"batch":{"dry_run":false,"scanned":1,"counts":{"protected":1},"reasons":{"lease_not_released":1},"next_cursor":"00000000-0000-0000-0000-000000000001"}}
```

Any `failed` count or checkpoint contention/failure stops with exit code 2. When `checkpoint_saved=false`, `batch.next_cursor` is **not durable progress**. Earlier successful deletions are not reversed. Investigate, then restart with **the same retention policy and scope**. Never bypass missing roots, modified files or protection states by clearing expiration metadata. Logs omit paths, filenames and raw database diagnostics.

SIGINT/SIGTERM cancels and joins in-flight work before closing the database pool. Signal-driven exit 0 does not mean a complete sweep. Slow synchronous file IO has no hard whole-run deadline; forced termination can happen between durable expiration intent and checkpoint commit. Restart safely rechecks the page.

## Explicit Linux service installation

The [service template](../../infra/systemd/kelpie-artifact-retention.service) is not automatically installed or enabled. It runs on the control host without KVM/libvirt groups or VM-host privileges. Operators must prepare and review:

- The installed API interpreter `/opt/kelpie/api/.venv/bin/python`, working directory `/opt/kelpie/api`, and unprivileged `kelpie-api` user/group. Explicitly adapt the template for different deployment paths.
- Existing API settings and PostgreSQL connection in protected `/etc/kelpie/api.env`. `ARTIFACT_ROOT` must match writable `/var/lib/kelpie/artifacts`. Different roots or SQLite database files require a separate path/permission review, not broad directory write access.
- Operator-owned, read-restricted `/etc/kelpie/artifact-retention.env`. The service scans all works by default, so approve that scope. For one work only, explicitly add the verified `--work-id` to `ExecStart`.

| Service-only variable | Default | Meaning |
| --- | --- | --- |
| `ARTIFACT_RETENTION_DAYS` | None; required | Approved retention period |
| `ARTIFACT_RETENTION_MODE` | `dry-run` | Set `apply` only after review |
| `ARTIFACT_RETENTION_LIMIT` | `100` | Candidates per batch |
| `ARTIFACT_RETENTION_INTERVAL_SECONDS` | `300` | Delay after a batch in seconds |

Systemd passes these variables as CLI arguments; they are not global API settings. Missing policy file/period prevents execution. On an approved host, install the reviewed template, check `systemd-analyze verify` and dry-run logs, and only then enable it. No additional timer is needed. `Restart=no` stops on failure: monitor service status, exit code and batch logs, then restart after investigation. SIGTERM has a 30-second grace period before potential forced termination. The macOS verification did not install or start a host service.

## Recovery and verification

Revision 0011 adds only the progress table. Database backup/clean restore tests include actual progress rows and refusal of writes by a read-only role. Restoring the DB/root at the same actual path and policy resumes that recovery point's progress. A changed root starts a new sweep while still respecting file-level expiration evidence. Stop this Worker as another DB/file writer during coordinated backup.

For rollback, first stop the service and prefer a forward fix preserving expiration reads, revision 0010 and audits. Downgrading only 0011 requires all new Workers stopped and coordinated deployment of code compatible with 0010. Losing checkpoints restarts scanning without removing completed deletion/audit evidence. Restoring expired bytes or dropping 0010 is not a rollback.

- Executable code `d35be34`, service `21eb4a6`: `make test` with real PostgreSQL passed 1,268 API, 45 Runner, 125 Web cases and Worker/Gateway tests. `make lint` passed. Subsequent service checks passed two cases on macOS; one Linux systemd syntax case is checked in that CI environment.
- Eleven real-process cases cover restart, periodic progress, SIGTERM during long waits, invalid input/configuration and private-output suppression. Seven PostgreSQL cases cover version races, retries, deletion/audit idempotence, stopping while waiting on a DB lock and connection return.
- An Orca browser with a disposable API/DB/files verified pre-expiration content, active download 200/completed download 410 after two separate Worker invocations, Korean/English expiration text without opening controls, and continued reading of protected content. Only 29 bytes of completed synthetic evidence were removed. Fixture ages/states are synthetic, not proof of actual VM execution.
- The 1155px Orca DOM had no horizontal overflow, and the final healthy-service interval had 72 completed requests without HTTP errors. Console messages were only DevTools/HMR notices. The native screenshot displayed the task terminal; clicking after focus restoration was rejected. Browser screenshots also timed out: **native input/screen-capture success is not claimed**. Post-expiration manual modal closing/reopening was not confirmed and remains separately covered by the existing automated regression.
- `artifact-retention.spec.ts` retains the manual CLI path and adds both locales after actual uploaded content expires through the scheduled Worker. All 41 Chromium cases for `cfbf890` passed on the first run in 2.5 minutes; 125 Web tests/type checking, lint and production build also passed. Actual Chromium screenshots at 390px Korean and desktop English were visually inspected and are retained in final CI's `browser-evidence`. Product UI code is unchanged.

Other data policies, object-store recovery, operational restore acceptance and physical KVM/network/Console isolation remain. This change does not mark all of OPS-001 or the MVP complete.
