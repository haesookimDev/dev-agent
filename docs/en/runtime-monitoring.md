# Continuous Worker, Lease, and queue observation

[한국어](../ko/runtime-monitoring.md) | English

## Scope and safety boundary

Independently of startup delivery recovery, the API reads Worker heartbeats, active lease expiry, and `queued` work from the database and exports `/metrics`. One aggregate SQL statement requests no per-work, organization, repository, Worker identifier, or credential labels. This observes database records; it does not prove real VM, network, or SCM health. It does not cancel, reapprove, retry, release leases, restart VMs, or clean up automatically.

The first observation starts immediately, followed by a ten-second delay after each attempt. Schema inspection, connection acquisition, query execution, and connection cleanup share a two-second deadline. Failures produce only a fixed warning and retry; a new immutable snapshot is published only after the entire read and cleanup succeed. An unready schema is unavailable. `/metrics` reads cached observations only, independently of database outages or delayed delivery recovery. Shutdown cancels and joins the task; a new lifespan clears previous observations.

Database overhead is a schema check and aggregate query approximately every 10–12 seconds per API process. PostgreSQL uses a coherent statement snapshot, without row locks, writes, migrations, or new dependencies. Queries exceeding the deadline on large tables are unavailable, not substituted with healthy values. Retain the existing [single API process delivery boundary](delivery-recovery.md). Summing the same global observations from multiple API instances double-counts them.

## Metric contract

Every name below has the `kelpie_runtime_` prefix. This is not a public API; restrict `/metrics` and the Prometheus UI/API to authorized internal networks.

| Suffix | Meaning |
| --- | --- |
| `snapshot_available` | 1 if the last attempt succeeded and the monotonic age of the last success is at most 30 seconds; otherwise 0 |
| `snapshot_age_seconds` | Monotonic time since the last success; `NaN` before any success |
| `snapshot_timestamp_seconds` | Unix time at the start of the last successful query; 0 before any success; diagnostic only |
| `workers{state}` | Registered Workers in fixed `online`, `draining`, `offline`, `quarantined` states |
| `leases{state}` | Lease rows whose DB state is `active`, split into `active` (expiry at/after observation) and `expired` (expiry before observation) |
| `queued_work` | Currently `queued` work; excludes approval, feedback, and input waits |
| `queue_oldest_age_seconds` | Age since creation of the oldest currently queued work, at observation time; 0 for an empty queue |

Worker classification takes precedence in this order: **quarantined → offline → draining → online**. A non-null `quarantined_at` is counted separately. Explicit `OFFLINE` or a heartbeat at least `WORKER_OFFLINE_SECONDS` old (existing setting, default 45 seconds) is offline. This setting classifies observations; it does not reclaim resources. Future heartbeats are not considered old until time catches up; future work creation times have age clamped to zero. Check operational clock synchronization too.

The lease condition `expires_at < observation time` matches existing lease validation. Released/quarantined lease rows are excluded from both counts. Queue age is measured from **creation**, not the last transition or retry. Running work and the entire pending/running `DeliveryJob` population are not monitored here.

Before the first success, Worker/Lease/queue metrics are **absent**. Only a successfully read empty database produces zero counts. After failure, previous values remain for diagnosis but `snapshot_available=0`; a stopped updater also becomes unavailable after 30 seconds. Neither old values nor startup recovery `completed=1` alone imply a healthy system.

## Alerts and response

Apply the current API and [rules](../../infra/monitoring/alerts.yml) together using the [base installation guide](monitoring-alerts.md). Existing 15-second scrapes, 30-second evaluations, and least-privilege CI remain unchanged. There are no new environment variables or automatic production deployments.

The `kelpie:runtime_snapshot_usable` recording rule checks success, freshness, current scrape success, and required state-series presence together. Partial metric relabeling also makes observations unavailable. New alerts are all warnings and fire after their condition is **continuously observed for two minutes**. They clear at the next evaluation after recovery. [Official alert-rule semantics](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)

| Alert | Condition |
| --- | --- |
| `KelpieRuntimeObservationUnavailable` | Scraping succeeds but the observation fails the success/freshness/required-metric contract |
| `KelpieWorkerHeartbeatLost` | A usable observation has offline Worker count > 0 |
| `KelpieActiveLeaseExpired` | A usable observation has expired active lease count > 0 |
| `KelpieWorkQueueStalled` | A usable observation has queued work and oldest age > 600 seconds |

Unavailable observations suppress individual domain alerts; failed scrapes defer to the existing scrape alert. Suppression does not mean the original fault is resolved. Normal values between observations or Prometheus restarts can reset pending time. Update rule tests together with thresholds/durations. External notifications require separate authorized Alertmanager configuration and receipt verification.

<a id="observation"></a>

### Unavailable observation

Check API version, metric relabeling, `snapshot_age_seconds`, `/readyz`, fixed observation warnings, database connections/pool/locks/migrations. `/readyz` checks schema revision and may miss an individual table query failure, so inspect observations too. Monitoring resumes after DB recovery without reapproval or a second API. Do not copy raw SQL, DSNs, or tokens into alerts or tickets.

<a id="heartbeat"></a>

### Lost Worker heartbeat

Use authorized administrator paths to inspect last receipt time, explicit state, and quarantine; check the Worker process, API network, and individual credential validity. Handle planned offline/draining periods through operational silence policy. Heartbeat receipt does not establish real VM or existing-connection safety; do not bypass with a shared secret.

<a id="lease"></a>

### Expired active lease

An authorized operator must inspect the lease's work, Worker, last event, and actual host state together. Expiry does not mean the VM stopped. Do not substitute SQL edits for the unimplemented active-VM termination/cleanup/forced-release workflow. A separate operational procedure must first prove termination and isolation to prevent double resource returns.

<a id="queue"></a>

### Long-queued work

Check the work list, resource requests, Worker capacity/draining/quarantine, claim errors, and organization/repository access. Ten minutes is a starting threshold to tune for actual load. When cancellation is needed, use only the supported [audited administrator cancellation of unassigned queued work](work-cancellation.md). Do not interpret approval waits as queue faults or remove approval gates.

## Validation and rollback

`make test-api` runs SQLite, metric freshness/failure, end-to-end timeout, shutdown, and real Uvicorn/HTTP recovery regressions. `KELPIE_TEST_POSTGRES_URL=<dedicated test database URL> .venv/bin/python -m pytest -q apps/api/tests/test_runtime_health.py` also verifies actual PostgreSQL aggregates, rolling back only a random schema inside each test transaction. Three PostgreSQL cases skip without the URL. Required `Python` CI runs this command on its existing PostgreSQL service.

`make test-monitoring PROMTOOL=/path/to/promtool` evaluates existing/new rules with a synthetic clock, including boundaries, missing/stale/NaN data, partial relabeling, other targets/jobs, suppression during observation failure, and recovery. The real HTTP regression retains the original ten-second interval and takes approximately 21 seconds; no extra CI job or external credential is required.

Roll back the API and dependent observation rules together, validate with `make test-monitoring`, and perform an authorized restart/reload. An older API lacks the new metrics, so leaving new rules in place produces an unavailable-observation warning. No data migration, audit deletion, or TSDB deletion is needed.

## Hands-on verification — 2026-09-06

Verified observation implementation `49e0e03`/lifecycle `e88357a`, rules `5045fbf`, HTTP regression `d1e54a4`, and CI `294d382`. Subsequent changes are Korean/English documentation only.

- `make test` with a dedicated PostgreSQL 17 URL: API 520 tests (66.92 seconds), Runner six, Web 52/type checking, Worker/Gateway passed. `make lint` passed.
- Prometheus 3.14.0 `make test-monitoring`: ten rules (nine alerts/one recording rule), 25 scenarios and 88 evaluation-time assertions passed.
- `npm run test:e2e --prefix apps/web`: 18 Chromium tests (1.5 minutes); `npm run build --prefix apps/web` passed. Existing cancellation, feedback, permission-error, bilingual, timezone, and event-stream journeys were revalidated.
- Hands-on acceptance ran migrated disposable SQLite/Uvicorn at `127.0.0.1:18470` and checksum-verified macOS ARM64 Prometheus at `127.0.0.1:19470`. Only the example target port and rule-file path changed; 15-second scrapes, 30-second evaluations, and two-minute pending time were retained.

| Directly exercised journey | Observed result |
| --- | --- |
| Start with synthetic stale heartbeat, expired active lease, and work queued 20 minutes ago | Three domain alerts Pending → Firing; observation remained available |
| Rename only the disposable DB's Worker table to induce an actual query failure | Old values retained with Available=0; unavailable-observation Pending → Firing; domain alerts suppressed; `/metrics` continued responding |
| Restore table, send individually authenticated heartbeat HTTP request, cancel queued work as administrator | Available=1 and Offline/Queue=0 without API restart; HTTP regression confirmed unrelated active lease remained unchanged |
| Mark the synthetic lease released only in the separate visual-acceptance fixture | Next observation/evaluation had zero active alerts, nine Inactive rules, target Up/no error |

Expanded each rule in the Orca browser to inspect Korean/English summaries, runbooks, and pending durations; refreshed actual states and the final desktop screen were also inspected through computer-use. All inspected browser requests returned 200 with no console messages. Screenshots were not publicly uploaded because they contain local paths. Web/native UI code is unchanged; native keyboard input was not verified because OS focus was unavailable.

This is evidence of synthetic Worker/lease records and actual HTTP observation boundaries, not a real Worker daemon, KVM, physical network isolation, or VM resource reclamation. No external notification receipt or production deployment occurred. The full MVP still needs external dependency readiness, stalled running-work/DeliveryJob coverage, an operations dashboard, retention/recovery, and verification on an authorized real KVM environment.
