# Control API alerts

[한국어](../ko/monitoring-alerts.md) | English

## Scope and contract

The [Prometheus rules](../../infra/monitoring/alerts.yml) detect the conditions below and include Korean/English response guides. They do not restart, reapprove, deliver, or delete data automatically.

| Alert | Condition | Severity |
| --- | --- | --- |
| `KelpieApiScrapeUnavailable` | Scraping a `kelpie-api` target fails continuously for two minutes | critical |
| `KelpieApiTargetMissing` | No `up` series for `kelpie-api` exists for two minutes | critical |
| `KelpieRecoveryMetricsMissing` | Scrapes succeed but the recovery `completed` phase series is missing for two minutes | warning |
| `KelpieStartupRecoveryStalled` | A scrapeable API's startup recovery remains unfinished for five minutes | warning |
| `KelpieDeliveryFailures` | An increase in delivery failures was observed in the last ten minutes and scraping currently succeeds | warning |

Aggregation removes the phase label so `waiting_for_database → running → retrying` does not reset the pending timer. `not_started` and scrapeable `cancelled` phases are unfinished too. [Recovery completion is not overall SCM success](delivery-recovery-metrics.md), so delivery failures are evaluated independently. During scrape failures, investigate the scrape alert instead of recovery/delivery alerts.

`for` measures continuous observation by Prometheus, not API boot time or the exact start of every fault. Scrape/evaluation intervals, stale-series disappearance, and Prometheus restarts affect detection time. Cleared conditions resolve at the next evaluation. Legitimate long deliveries can trigger the five-minute warning; adjust thresholds and tests together to match normal deployment behavior. [Official rule semantics](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)

Failures before the first counter scrape, processes that start and stop between scrapes, and failures with only an initial value and no observed increase may be missed. A counter reset alone is not a new failure. Alerts do not guarantee every event's delivery or replace jobs/events/audits. [Continuous Worker heartbeat/lease/queued-work observations and unavailable-observation alerts](runtime-monitoring.md), added to the same rule file, have their own contract and response guide. External object-store/SCM readiness, stalled running-work/DeliveryJob coverage, and an integrated operations dashboard remain.

## Installation and validation

Use official `promtool` as a development/operations tool to validate actual PromQL semantics. No application package dependency is added. Download the matching architecture archive from the [official 3.14.0 release](https://github.com/prometheus/prometheus/releases/tag/v3.14.0) and verify SHA-256 before extraction.

| Archive | SHA-256 |
| --- | --- |
| `prometheus-3.14.0.linux-amd64.tar.gz` | `f665c6da19eb7ba399c915d30c7d9793c9b417bf8a749b504bc470678631478d` |
| `prometheus-3.14.0.darwin-arm64.tar.gz` | `a9623f7f4fe65b1b171b423c1a72bbf23dfdf41a171dcb33e7dd302af80dc01c` |

From the root, run `make test-monitoring PROMTOOL=/path/to/promtool`. `PROMTOOL` is a Make variable specifying the executable, not an API environment variable. The command validates configuration/rule syntax and duplicate-rule lint, then evaluates synthetic series using PromQL without waiting five or ten real minutes. Existing `make test` retains application coverage. Required CI check `Go` includes monitoring validation; the archive is cached by version/checksum and its pinned SHA-256 is verified before extraction even on cache hits. [Official rule testing](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)

## Execution and operational setup

The [example configuration](../../infra/monitoring/prometheus.example.yml) scrapes `127.0.0.1:8000/metrics` on the same host every 15 seconds with a three-second timeout and evaluates rules every 30 seconds. Elsewhere, replace the target with an authorized internal API but retain `job_name: kelpie-api`. Keep configuration and rules together. Default Compose, service autostart, and production deployments are unchanged.

Prepare a writable **dedicated** data directory and use the official executable. Replace the example directory below with its actual path.

```sh
prometheus --config.file=infra/monitoring/prometheus.example.yml \
  --web.listen-address=127.0.0.1:9090 \
  --storage.tsdb.path=/path/to/dedicated-monitoring-data \
  --storage.tsdb.retention.time=7d --storage.tsdb.retention.size=1GB
```

At `http://127.0.0.1:9090/alerts`, expand rules to inspect pending/firing states, labels, Korean/English summaries, and runbooks. Use `/targets` for target health and `/query` for `up{job="kelpie-api"}` and `ALERTS{job="kelpie-api"}`. This is the Prometheus UI, not a new Kelpie dashboard. Storage limits remove old **monitoring series**; they do not implement work/artifact retention.

The example sends no external notifications. For production notifications, separately configure an authorized Alertmanager's internal HTTPS address, authentication, receivers, grouping/deduplication, and maintenance silences, then verify actual receipt. Use existing secret providers/access controls, never tokens in configuration, documents, or screenshots. Restrict the Prometheus UI/API and `/metrics` to internal networks; do not disable TLS verification or use a public listen address. [Official configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)

To roll back, restore and validate previous monitoring configuration/rules, then apply a normal configuration reload or restart. Rolling the API back before recovery metrics existed also requires rolling back dependent rules. No API data migration or TSDB deletion is needed.

## Response guides

<a id="api-scrape"></a>

### Failed API scrape

Inspect the Prometheus `/targets` error, API process, internal DNS/network, and proxy restrictions. Check `/healthz`, `/readyz`, and `/metrics` separately over an authorized internal path. A failed scrape does not directly prove a DB outage. Do not copy tokens/raw connection strings or remove network restrictions.

<a id="missing-target"></a>

### Missing target

Check `job_name: kelpie-api`, deployed configuration/service discovery, and the intended single API target. Another job's `up=1` does not prove Kelpie health. Use approved maintenance silences for planned downtime; do not delete rules to conceal outages.

<a id="missing-metrics"></a>

### Missing recovery metrics

Check that the target is Kelpie, its version includes [recovery metrics](delivery-recovery-metrics.md), and metric relabeling is not dropping them. The `completed` **series must exist**; zero still meets the contract. An older API rollback needs compatible rules too.

<a id="startup-recovery"></a>

### Delayed startup recovery

Inspect phase, elapsed time, `checks_total`, and `/readyz` together. Check connectivity/migrations for an unready DB, fixed warnings/DB locks/pool state for repeated errors, and jobs/events/SCM access/normal duration for long deliveries. Restoring the database needs no further API restart or reapproval. Do not reapprove or start a second API arbitrarily because delivery may duplicate; retain the [single-process boundary](delivery-recovery.md).

<a id="delivery-failures"></a>

### Delivery failures

Inspect final work/DeliveryJob state, [safe stage/error codes](delivery-failure-safety.md), the verified bundle, and approval history. Check GitHub installation/repository permissions or temporary SCM outages, but never put tokens, patches, or raw exceptions in alerts. Distinguish recovery completion from individual failure. This rule grants no authority to automatically retry or bypass approval/quarantine gates.

## Verification record

The following records the original baseline-alert rollout. See [runtime observation verification](runtime-monitoring.md) for the added continuous monitoring.

Verified rules `43af5cf`, test command `04ed249`, and CI `126337f`. Subsequent changes in this PR are instructions/documentation only.

- Prometheus 3.14.0 `make test-monitoring`: five rules, example configuration, nine scenarios, and 27 evaluation-time assertions passed. Coverage includes phase-stable pending time, completion/recovery, missing targets/metrics, other-job exclusion, counter resets/first-observation limits/failure-window expiry, and stale metrics.
- `KELPIE_TEST_POSTGRES_URL=<isolated PostgreSQL 17> make test`: API 367 (37.02 seconds), Runner six, Web 40/type checking, Worker/Gateway passed. `make lint` passed. Workflow YAML parsing/read-only permissions, document links, and ten bilingual runbook anchors were checked.
- Direct acceptance used Uvicorn with a migrated disposable SQLite database and checksum-verified macOS ARM64 Prometheus on loopback. Only the example's target port/rule-file path changed: 15-second scrapes, 30-second evaluations, and actual five/two-minute conditions remained intact.

| Directly exercised journey | Observed result |
| --- | --- |
| Start API with an unready schema revision in the isolated database | Actual metrics scraped successfully; delayed recovery Pending → Firing |
| Restore only the owned schema marker | Recovery completed=1 without API restart/reapproval; alert cleared |
| Stop only the owned API process | Connection refused/Down in `/targets`; failed scrape Pending → Firing, no recovery alert |
| Restart API | `/readyz` 200, target Up/no error, zero active alerts and five Inactive rules |

Rules were expanded in the Orca browser to inspect Korean/English summaries, runbooks, firing states, and the final inactive screen. Computer-use also inspected the corresponding desktop screen. Native input was not tested because focus was unavailable; native/Web UI code is unchanged. All 40 captured browser requests returned 200 with no console messages. Prometheus's deliberate failed API scrape was inspected separately in `/targets`.

Delivery-failure/missing-metric conditions use synthetic-series tests, not actual external SCM, notification-receiver, worker, or VM evidence. No external notifications or production deployment occurred. Screenshots include local paths and were not publicly uploaded. Owned API/Prometheus/tab/terminal and disposable SQLite/TSDB/logs were cleaned up; verification data can be recreated by rerunning. User processes/databases were unchanged.
