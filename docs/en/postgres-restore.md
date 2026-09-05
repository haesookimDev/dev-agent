# PostgreSQL backup and restore verification

English | [한국어](../ko/postgres-restore.md)

## Scope and safety boundaries

This covers OPS-001's logical-backup/new-database restore runbook and repeatable verification. It is not production, object-store, or VM recovery/deployment. Scheduled backups, PITR, encrypted-storage provisioning, and realistic RPO/RTO measurement remain separate operational work.

`pg_dump` takes a consistent snapshot of one database, excluding cluster roles, tablespaces, and external files. Use `pg_restore --single-transaction --exit-on-error` to roll back partial restoration on failure. [SQL dump](https://www.postgresql.org/docs/17/backup-dump.html), [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html), [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

- Backups contain sensitive requirements, audit identities, OIDC login verifiers, and session/worker/lease hashes. Custom format and SHA-256 provide neither encryption nor author authentication. Use approved encrypted storage, access controls, and retention; never upload dumps, logs, or credentials to Git, PRs, or CI artifacts.
- Restore only trusted backups: functions and other dump content can execute SQL during restoration. Do not remove TLS verification, original ownership/ACLs, or audit triggers to make verification pass.
- Preserve the source. An operator must resolve the cluster, connection role, and new DB name. Do not work around errors with `--clean`, existing-database overwrites, `--disable-triggers`, `--no-owner`, or `--no-acl`.
- Isolate recovery from production API/worker/webhook/preview traffic and external SCM/notifications. Once ready, the API resumes retained `pending`/`retry`/`running` deliveries; do not start with its normal write account before verification. [Delivery recovery](delivery-recovery.md)

## Backup

1. Close intake and coordinate API, worker, and file writers to establish the DB/file recovery point. Verify termination and actual remote outcomes of in-flight VMs/transfers. A database snapshot does not stop them.
2. Record the deployed API commit/image digest, Alembic revision, PostgreSQL/client versions, UTC time, cluster identity, DB owner/roles/database settings/connect ACLs/tablespaces, and matching object-store snapshot/versions in encrypted inventory. Application organization roles are not PostgreSQL cluster roles.
3. Use compatible, matching-major `pg_dump`/`pg_restore` clients; regression uses PostgreSQL 17 clients/server. Point `PGSERVICEFILE`/`PGPASSFILE` at restricted secret-manager-injected files, not passwords/DSNs in argv. Production services must use `sslmode=verify-full` with a verified CA.
4. These services/mounts are operator-approved, pre-provisioned examples. They are not created automatically; do not blindly use them as real targets. Check every exit status.

```sh
set -eu
umask 077
backup_dir=$(mktemp -d /run/secure-backups/kelpie.XXXXXX)
pg_dump --no-password --dbname='service=kelpie-backup' --format=custom \
  --file="$backup_dir/control.dump" 2>"$backup_dir/dump.log"
pg_restore --list "$backup_dir/control.dump" >"$backup_dir/catalog.txt"
(cd "$backup_dir" && sha256sum control.dump >control.dump.sha256)
```

Insufficient privileges and dump errors are failures. Do not silently exclude data with table/schema selection. Link the hash, size, recovery point, and success result to inventory, then read back from approved storage to verify. On macOS use `shasum -a 256` instead of `sha256sum`.

## Restore into a new database

Provision inventory roles/tablespaces on a cluster separate from the source. Role definitions/credentials, database settings, and connect ACLs are not recovered by this single-database dump alone. Do not broaden privileges. Retrieve the backup and compare it with the trusted inventory hash.

Replace `restore_database` and the owner below with an operator-confirmed **nonexistent** DB name and pre-existing role. Stop immediately if `createdb` fails; do not delete existing databases or reuse a failed name.

```sh
set -eu
umask 077
restore_database=kelpie_restore_incident_001
(cd "$backup_dir" && sha256sum --check control.dump.sha256)
createdb --no-password --maintenance-db='service=kelpie-restore-admin' \
  --template=template0 --owner=kelpie_owner "$restore_database"
pg_restore --no-password \
  --dbname="service=kelpie-restore-admin dbname=$restore_database" \
  --single-transaction --exit-on-error "$backup_dir/control.dump" \
  2>"$backup_dir/restore.log"
```

Restore database settings/connect ACLs while keeping external access closed. On failure, isolate the new database, resolve the cause, and retry with another new database. The single transaction does not undo database creation. Do not automatically delete databases or terminate production connections.

## Verification and resumption gates

1. Using the backup's API version, verify the Alembic head and run `alembic check`. This alone does not establish restored triggers, permissions, or data. If needed, complete verification before a separate [migration](operations.md#database-migration).
2. Compare all table counts/private fingerprints, PK/FK, columns/defaults/indexes, next sequence IDs, organizations/repositories/grants, audit details, and approval-to-DeliveryJob links. Never print raw rows to logs.
3. Verify owners/ACLs and quarantine/revocation state. In transactions on a disposable restored copy, check rejection of audit UPDATE/DELETE/TRUNCATE/upsert and forged service actors, plus collision-free new audit INSERTs. Never rewrite/delete retained audits.
4. Compare actual artifact/delivery-bundle bytes, sizes, and hashes with matching snapshots. The API returns `410 artifact content is unavailable` when only metadata exists. Reconcile actual preview targets, VMs, and leases too.
5. Reconcile post-backup membership/grant revocations, invalidated sessions, worker rotation/quarantine, changed approvals, and existing remote commits/PRs against trusted current records. Do not reopen traffic with resurrected sessions/tokens/approvals. Never guess missing approval provenance or blindly retry uncertain delivery.
6. Record gate evidence, data-loss interval (RPO), elapsed recovery time (RTO), rollback target, and operational approval. Confirm the old API is fully stopped; gradually resume one API, workers, and intake. `/readyz` 200 does not prove delivery/file/VM recovery. Run `ANALYZE` in isolation if needed.

Before cutover, abandon a failed candidate while preserving the source. After new writes, blindly switching back can lose data or duplicate delivery: close intake again and reconcile changes. Autonomous repository PR merge authorization does not authorize production DB cutover/deletion.

## Repeatable verification and CI

Set `KELPIE_TEST_POSTGRES_URL`/`KELPIE_TEST_POSTGRES_CONTAINER` to the same dedicated PostgreSQL 17 server. The **test-only** account needs DB/role creation privileges and local authentication inside the container. Never use production targets.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_postgres_restore.py \
  apps/api/tests/test_postgres_restore_runtime.py
```

Six tests only use successfully created `kelpie_restore_<UUID>` databases and `kelpie_reader_<UUID>` roles. The input URL's database is only an administrative connection, never a migration/dump/restore/deletion target. Existing-name collisions confer no ownership. Cleanup does not force-close connections and removes only owned databases, roles, 0600 archives, and logs.

- Seed every model table; run actual Alembic upgrade → custom dump → new `template0` database restore → compare all rows/schema/owners/ACLs/sequences and run Alembic check.
- Normalize only equivalent literal-array casts reparsed by PostgreSQL and explicit/implicit default ACL representations. Preserve other constraint expressions and exercise audit rejection through actual SQL too.
- Conflicting tables/truncated archives must fail without leaving preceding DDL/data behind.
- Start actual Uvicorn with a SELECT-only PostgreSQL login. Check a restored synthetic OIDC session's own-organization reads, other-organization 404, administrator-audit 403, unauthenticated 401, and missing-artifact-bytes 410. Startup delivery recovery really runs but cannot acquire write locks; source/restored fingerprints remain unchanged. This is not a product maintenance mode or a way to expose write APIs.

Without the environment variables these six tests skip. Required `Python` CI uses the existing PostgreSQL service's [container ID](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context) to run without skips. No job, matrix, external secret, or dependency was added; existing eight-minute timeouts and read-only workflow permissions remain.

## Hands-on evidence

On 2026-09-06 KST, tests/workflow at `a47856c` and the unchanged application passed six actual PostgreSQL restore tests (3.40 seconds), dedicated-PostgreSQL `make test` (526 API tests/70.99 seconds, six Runner tests, 52 Web tests/type checking, Worker/Gateway), and `make lint`. PostgreSQL skips in default SQLite runs do not replace actual restore verification.

Browser evidence and final commit/CI/merge results are recorded in the PR. Tests use new databases within the same test cluster and pre-provisioned test roles. They do not prove cross-cluster role/configuration migration, real IdP login, production recovery, object-store/VM restoration, or completion of the entire MVP.
