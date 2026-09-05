# Per-worker credential operations

[한국어](../ko/worker-credentials.md) | English

Worker authentication defaults to `WORKER_AUTH_MODE=scoped`. Registration, heartbeat, and claim require a control-host-issued credential bound to one Worker name and ID. Another Worker returns `403`; invalid, expired, or revoked tokens return `401`. Task VMs/runners receive only their work lease token, never a Worker credential.

## Issuance and initial migration

1. Drain existing Workers, confirm active work and VM cleanup are finished, and stop them during maintenance. Issuance immediately blocks shared-token access for that Worker; initial migration is not zero-downtime.
2. Back up the database and run `alembic upgrade head` to apply `20260905_0005`. The CLI requires database authority on the control host and a ready schema; there is no public HTTP issuance endpoint.
3. From the repository root, issue into an existing access-controlled directory. These are example paths; never pass actual tokens as arguments.

```bash
.venv/bin/python -m app.worker_admin issue \
  --worker-name worker-1 --reason 'Provision new Worker' \
  --output /secure/provisioning/worker-1.token
```

The CLI writes plaintext only to a new `0600` file. Stdout contains only Worker ID, credential ID, and expiration time. Existing files/symlinks are never overwritten. A file-write failure prevents database commit; a commit error removes the file created by that invocation. After forced termination or an uncertain commit outcome, inspect metadata and revoke unwanted credentials. The filesystem and database are not a distributed atomic transaction.

4. Use an approved secret delivery channel to place the file only on its target host. Grant the Worker service account read access with `0400`/`0600` permissions and restricted directory access. Mount the protected directory read-only in containers, not a single file, to allow atomic replacement. Remove temporary plaintext control-host copies according to retention policy after distribution.
5. Start the API with `WORKER_AUTH_MODE=scoped` and the Worker with the configuration below. The name must exactly match issuance. An arbitrary 32-character token is not a scoped credential.

```dotenv
KELPIE_WORKER_NAME=worker-1
KELPIE_WORKER_TOKEN_FILE=/run/secrets/kelpie/worker-1.token
```

A configured file overrides `KELPIE_WORKER_TOKEN` without falling back to an old/environment value on failure. Registration, heartbeat, and claim reopen it each time. It must be a regular file, at most 64 KiB, containing at least 32 whitespace-free ASCII characters after trailing CR/LF removal. Missing/empty files, directories, FIFOs, and read failures prevent requests without logging paths or plaintext.

## Rotation and individual revocation during execution

```bash
.venv/bin/python -m app.worker_admin list
.venv/bin/python -m app.worker_admin rotate \
  --credential-id <current-credential-ID> --reason 'Scheduled rotation' \
  --overlap-seconds 600 --output /secure/provisioning/worker-1.next
```

Securely deliver the new file to the target host, then atomically replace the destination with a completed file on the same filesystem. Verify successful heartbeat/claim requests and an updated `last_used_at` for the new credential in `list`. No Worker restart is required. Then revoke the old credential.

```bash
.venv/bin/python -m app.worker_admin revoke \
  --credential-id <previous-credential-ID> --reason 'Replacement usage confirmed'
```

- Lifetime defaults to 30 days; `--lifetime-seconds` accepts 60 seconds–90 days. Overlap defaults to 600 seconds and accepts 60–3600 seconds. The old token's original expiration is never extended.
- Replacement tokens belong to the same Worker. `revoke` affects only the specified credential and is idempotent without duplicate events. Other Workers and replacement tokens remain valid.
- Routine rotation/revocation does not invalidate active work leases. **This is not a compromised-host quarantine command.** VM termination, WireGuard isolation, Preview connection termination, and bulk lease invalidation remain follow-up incident-response work. Do not consider a compromised host contained by this feature alone.
- The database stores only SHA-256 token hashes. Management events record OS UID, reason, and time. This is not an audit store immutable to database administrators; shared OS accounts cannot identify individuals.

## Gateway separation and development compatibility

Preview resolution no longer accepts Worker tokens. Supply a separate matching random value to the API's `GATEWAY_SECRET` or `GATEWAY_SECRET_FILE` and the Gateway's `KELPIE_GATEWAY_TOKEN`. It must contain at least 32 whitespace-free ASCII characters and cannot reuse a Worker's `kwc_` token. The API reloads its file, but the Gateway reads its environment at startup and needs a coordinated restart for rotation. Gateway authentication still defaults to `disabled`; do not expose it before OIDC Preview grants are implemented.

Shared tokens are accepted only in isolated local demos explicitly setting both `AUTH_MODE=development` and `WORKER_AUTH_MODE=development`. OIDC mode rejects shared authentication at startup. Once a Worker receives an individual credential, revoking every credential does not restore shared-token access. Default Compose uses this explicit demo exception; browser E2E uses the real management CLI and scoped token files.

## Migration and rollback

The migration preserves existing Workers and resource reservations while adding credential/event tables, an individual-credential-required flag, and a quarantine timestamp. The timestamp supports authentication blocking; operational bulk quarantine is not implemented yet. Request/response bodies are unchanged, but authentication and the Gateway environment rename are breaking changes.

Roll back only during maintenance with external traffic and Workers blocked. Back up the database before `alembic downgrade 20260905_0004`, which deletes credential, history, and quarantine metadata. Re-upgrading cannot recover it; issue new credentials. Do not reopen production traffic on an older API lacking scoped authentication; roll forward to a corrected version. Never recover production with development shared tokens.

## Verification

- `make test-api`: real CLI, output protection, permissions, failure cleanup, Worker binding, rotation/revocation/expiration, Gateway separation, and shared-token bypass prevention.
- Set `KELPIE_TEST_POSTGRES_URL` to an isolated PostgreSQL database with the current schema and run `pytest -q apps/api/tests/test_worker_postgres.py`: both authentication/revocation races and independent Worker progress. Only these two tests skip without that URL; CI always runs them in a dedicated step.
- `make test-worker` and `go test -race ./...` in the Worker directory: atomic replacement, fail-closed missing files, and active work lease separation.
- `npm run test:e2e --prefix apps/web`: creation, feedback, re-verification, and approval through a real API and scoped-authenticated Mock Worker. Mock verification is not evidence of isolation between two real KVM/browser VMs.
