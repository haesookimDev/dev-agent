# Startup delivery recovery metrics

[한국어](../ko/delivery-recovery-metrics.md) | English

## Scrape contract

`GET /metrics` adds the following to existing metrics. Scraping does not query the database or SCM, so metrics remain available when the database is unresponsive. Do not expose this endpoint publicly; [restrict it to the internal Prometheus network](operations.md#observability-and-correlation).

| Name | Type and labels | Meaning |
| --- | --- | --- |
| `kelpie_delivery_startup_recovery_state` | Gauge; a label with the same name contains one of six phases below | Current phase is 1; all others are 0 |
| `kelpie_delivery_startup_recovery_duration_seconds` | Gauge; no labels | Elapsed time from scheduling recovery to completion/cancellation |
| `kelpie_delivery_startup_recovery_checks_total` | Counter; `outcome` = `database_unready`, `completed`, `error` | Outcomes of finished readiness/recovery iterations, not individual job counts |

- `not_started`: recovery has not been scheduled; elapsed time is zero.
- `waiting_for_database`: recovery is scheduled and checking database/schema readiness or waiting because it is unready.
- `running`: awaiting the scan and delivery processing on a ready database.
- `retrying`: a readiness/recovery exception occurred and a retry is pending. The previous phase can remain visible during the next readiness check.
- `completed`: the recovery function returned. This may include an empty queue, individual delivery failures, quarantined jobs, or skipping jobs already active in this process.
- `cancelled`: API shutdown cancelled unfinished recovery. The endpoint may be gone after shutdown; do not assume every cancellation can be scraped.

Elapsed time uses a monotonic clock and includes background readiness checks, database waits, and five-second retry backoffs. It excludes the initial synchronous startup schema check, so it is not total API boot time. It freezes after completion/cancellation. Reopening a lifespan in the same process resets phase and duration but retains counters. Restarting the process resets counters too. The existing Prometheus client also exports counter creation timestamps as `kelpie_delivery_startup_recovery_checks_created`.

For example, a response after one completed recovery contains:

```text
kelpie_delivery_startup_recovery_state{kelpie_delivery_startup_recovery_state="completed"} 1.0
kelpie_delivery_startup_recovery_checks_total{outcome="completed"} 1.0
```

Do not interpret `completed=1` as successful SCM delivery or continuous queue health. `/readyz` also checks only the schema. Inspect individual outcomes through existing `kelpie_delivery_outcomes_total`, `delivery_jobs.state/attempts`, and work status/events. Alerts for missing scrapes or prolonged recovery waits/errors, throughput/latency SLOs, and continuous queue monitoring remain follow-up scope.

These metrics cover [one API process](delivery-recovery.md), not multi-worker aggregation or distributed recovery. Labels contain fixed phases/outcomes only, never job, repository, user, request IDs, or raw exceptions. There are no new dependencies, environment variables, or migrations; existing API responses and metric contracts remain intact. Rolling back the API removes the new metrics, so revert dependent collection rules/dashboards as well. No data downgrade is required.

## Verification record

Verified implementation `3e3a833` and real-process coverage `e7cf5cd`; subsequent changes in this PR are documentation only.

- `KELPIE_TEST_POSTGRES_URL=<isolated PostgreSQL> make test-api`: 367 passed in 37.62 seconds. `make lint` passed. The ten new metric tests and two real-process tests also passed separately.
- Unit coverage includes initial phase, fixed labels, monotonic elapsed time, completion, cancellation, repeated lifespans, DB wait→error→running→completed, immediate cancellation before the task runs, and `/metrics` without DB access.
- A real Uvicorn/SQLite test observes three individual delivery failures while recovery completion counts once and recovery errors remain zero. A real Uvicorn/unresponsive TCP test confirms `/metrics` returns 200 within a 0.5-second request timeout while `/readyz` is pending. Both run in default API CI without additional services.

Direct acceptance ran a dedicated PostgreSQL 17 database, fault-injecting TCP proxy, and real Uvicorn on loopback. The empty queue verifies recovery observation only; it is not evidence of external GitHub writes, actual SCM delivery, workers, or VMs.

| Directly introduced condition | Observed in the browser |
| --- | --- |
| Unresponsive DB proxy | `/metrics` 200, `waiting_for_database=1` |
| Restore connectivity, retain `delivery_jobs` table lock | `/readyz` 200 while observing `running` and `retrying`; retry-phase scrape 200 in 3ms |
| Release owned lock | `completed=1` without restarting API; one completion, 11 unready checks, 34 recovery errors |
| Scrape again after completion | Duration frozen at approximately 317.138 seconds, including the deliberately prolonged fault |

The actual response/final screen were inspected in the Orca browser and the desktop window through Computer-use. Native input was not tested because native focus was unavailable. This change does not modify Web or native-input UI. The synthetic DB password was absent from API logs/metrics. Owned API/proxy/lock/tab/terminal, temporary logs, and disposable database were cleaned up. This verification-only database can be recreated through migrations; user databases and processes were unchanged.
