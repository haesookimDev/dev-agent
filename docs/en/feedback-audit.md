# Feedback audit records

[한국어](../ko/feedback-audit.md) | English

## Scope and security boundary

This is the first IAM-001 audit batch. Successful web feedback and Slack feedback that passes signature verification and principal linking append `feedback.created` to `audit_records`. The feedback, activity events, state transition and audit use one database transaction; audit failure rolls everything back. Permission denial, closed work and quarantined workers do not produce success records.

- Principal ID, subject, identity provider, organization/repository and the actual organization role, repository grant, effective role and required role are retained as decision-time snapshots. Development authentication explicitly uses `identity_provider=development`, `actor_id=null`.
- Request ID matches the response's `X-Request-ID`; correlation ID belongs to the work. Clients can supply valid request UUIDs, so request IDs are neither identity proof nor deduplication keys. The database assigns a separate audit row ID.
- `source_ip` is the API's ASGI client IP, or `null` when missing/not an IP. This code does not interpret `X-Forwarded-For`. The ASGI server may already have transformed it under its trusted-proxy configuration; operators must restrict that trust. A direct server with proxy headers disabled records the connection peer; relayed requests may identify the relay. Slack's address is not the human user's device IP.
- `transport` comes from the actual endpoint, not the request's `channel`. Audit rows do not copy feedback text, cookies, bearer tokens or arbitrary payloads. Existing activity-log text retention is unchanged. Creation timestamps are UTC.
- Snapshots have no live-resource foreign keys/cascades and survive work, feedback or membership cleanup. Historical activity events are not backfilled as trusted audits.

Database triggers reject SQLite UPDATE/DELETE/REPLACE/UPSERT and PostgreSQL UPDATE/DELETE/TRUNCATE/UPSERT. Explicit development bootstrap installs the same guards. This is **append-only protection for ordinary DML**, not external WORM storage or cryptographic proof against database-owner/superuser DDL, disabled triggers or database-file replacement. Database administrators remain trusted; runtime database accounts should not have DDL/trigger-management authority. This change does not provision database accounts or alter existing privileges.

References: [SQLite triggers](https://www.sqlite.org/lang_createtrigger.html), [REPLACE conflict handling](https://www.sqlite.org/lang_conflict.html), [PostgreSQL 17 triggers](https://www.postgresql.org/docs/17/sql-createtrigger.html), [SQLAlchemy DDL hooks](https://docs.sqlalchemy.org/en/20/core/ddl.html).

## Read API

`GET /api/work-items/{id}/audit-log?after=0&limit=100` requires current organization or repository administrator authority. Other-organization/missing work returns 404; insufficient permission returns 403. `after >= 0`, `1 <= limit <= 1000`, default 100; pass the last row ID as the next `after`. Responses are ascending-ID JSON arrays with `Cache-Control: no-store`. There are no HTTP append/update/delete endpoints.

Selected fields from a synthetic response (full contract at `/openapi.json`):

```json
{
  "id": 1,
  "action": "feedback.created",
  "target_id": "1",
  "actor_subject": "demo-user",
  "identity_provider": "https://identity.example",
  "organization_role": "viewer",
  "repository_role": "approver",
  "effective_role": "approver",
  "required_role": "operator",
  "transport": "slack"
}
```

`target_id` identifies the feedback row. This adds a read endpoint without changing existing feedback requests/responses or activity events. Deleting work preserves audit rows, but work-scoped HTTP reads return 404; retained-record investigations require separately authorized database access. An audit UI and retention/export tooling are outside this batch.

## Migration and rollback

1. Back up the database, run `make migrate-api` to apply `20260906_0006` before API rollout, then run Alembic `check`. No new environment variables or dependencies.
2. Online downgrade is allowed only for an empty audit table. A PostgreSQL table lock or SQLite writer lock protects the check. Existing records cause refusal, preserving data and revision. Offline downgrade is also refused.
3. Once records exist, retain the current schema/guards and recover with a forward fix, not data deletion or manual revision stamping. Older APIs do not consider the newer schema ready, so binary rollback alone cannot restore service. Establish an approved retention/archive/recovery procedure first.

## Verification · 2026-09-06

Implementation `30f866d`, browser tests `5d4a444`, CI `3fb75b8`. Subsequent documentation/evidence changes do not alter runtime behavior.

- `make test`: API 261, Runner 6, Worker/Gateway Go tests, Web 40 and TypeScript passed. The 17 PostgreSQL tests skipped by the default run all passed separately using the dedicated URL with `test_worker_postgres.py test_audit_postgres.py`.
- `make lint`, production Web build and 12 Chromium E2E tests passed. PostgreSQL 17 upgrade/check/downgrade/re-upgrade confirmed no schema differences. Eight audit tests were added to the existing Python CI job; local PostgreSQL audit execution took about 0.6 seconds.
- Isolated SQLite API `:18520` and production-mode Web `:13520`: created work and submitted feedback through Orca's Korean page, then submitted a second feedback through native Chrome's English page. Verified success feedback/live activity, two audit rows with distinct request IDs, no copied message text and `no-store` reads. Orca console had no errors; read/create/feedback/SSE requests returned 2xx.
- On the same real services, KO/EN × 1440/390px views passed input-label, no-overflow, first keyboard target/skip-link focus and page-error checks; captured images were inspected. There is no UI implementation change; these are usage evidence. Local development authentication does not substitute for real OIDC-provider, Slack-service or VM verification.

![Korean desktop feedback activity](../assets/feedback-audit/ko-desktop.png)
![English narrow-screen feedback activity](../assets/feedback-audit/en-mobile.png)

The next audit batch covers console ownership, approvals, cancellation and delivery. Denied-attempt auditing, audit retention/external archiving, OIDC preview grants, real KVM/WireGuard/concurrent-VM isolation and other remaining MVP items are not marked complete.
