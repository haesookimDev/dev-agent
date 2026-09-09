# Execution and delivery metadata-age observation

[한국어](../ko/execution-monitoring.md) | English

## Scope and metric contract

The API extends the [read-only runtime snapshot](runtime-monitoring.md) with execution-phase and durable `DeliveryJob` counts and ages. It uses the same coherent aggregate SQL, ten-second delay after each attempt, two-second total deadline, immutable cache and thirty-second freshness boundary. `/metrics` never queries the database. No new dependencies, environment variables, schema migrations, lifecycle writes or automatic remediation are introduced.

All names have the `kelpie_runtime_` prefix and only a fixed `state` label:

| Suffix | States and meaning |
| --- | --- |
| `execution_work{state}` | Counts in `provisioning`, `analyzing`, `implementing`, `verifying`, `committing`, `pr_created` |
| `execution_oldest_update_age_seconds{state}` | Age of the oldest `WorkItem.updated_at` in each of those states |
| `delivery_jobs{state}` | Counts in `pending`, `retry`, `running`, `completed`, `failed`, `unknown` |
| `delivery_oldest_update_age_seconds{state}` | Age of the oldest `DeliveryJob.updated_at` in each of those states |

There are 24 additional series per API instance, without organization, repository, work/Worker identifiers, raw errors or credentials. Unrecognized delivery states aggregate into `unknown`; raw state text never becomes a label. Human approval, feedback, input and budget waits, queued and terminal work are excluded from execution counts. Completed/failed delivery history is observable but does not trigger an ongoing-job stall alert.

Age means **metadata update age**, not time since the last AgentEvent, phase entry, actual progress, or proof that a VM stopped. Other metadata updates can reset the age; long healthy phases can warn. Queue age retains its separate **creation-time** contract. Empty successfully observed states explicitly have count/age zero; future update times clamp age to zero. Before first success all domain series are absent. Failed attempts retain old values with `snapshot_available=0`; stale/failed values are not healthy observations.

Keep `/metrics` and Prometheus UI/API on authorized internal networks. These are global observations: do not sum identical snapshots from multiple API instances. Large-table scans share the existing deadline; timeout is unavailable, not an empty fleet. The existing single-process delivery deployment boundary remains unchanged.

## Alerts and safe response

Use the [installation guide](monitoring-alerts.md) and roll out the API with the matching [rules](../../infra/monitoring/alerts.yml). Scrapes remain 15 seconds, evaluations 30 seconds. All three alerts are warnings with a continuously observed **two-minute** pending duration; they clear at the next successful evaluation after their condition clears. No external Alertmanager receiver or production deployment is configured by this change.

The `kelpie:execution_snapshot_usable` recording rule requires the existing runtime gate and exactly one finite, nonnegative series for every required state in all four new metric families. Missing, stale, negative, NaN, infinite or duplicate state series cannot be substituted with zero. Other jobs/targets cannot supply a missing state. The gate is separate so losing only new metrics does not suppress valid Worker/lease/queue alerts.

<a id="observation"></a>

### `KelpieExecutionObservationUnavailable`

The base runtime observation is usable but the extended execution contract is not. Check API/rule version alignment and metric relabeling, then the four complete six-state families for that target. Restore intended collection rather than inventing zero series or disabling the gate. Whole-observation or scrape failure uses the existing runtime/scrape alert instead; absence of a stall alert then does not prove recovery. A successful `/readyz` alone does not prove that all observation tables can be queried.

<a id="execution"></a>

### `KelpieExecutionPhaseStalled`

At least one work item is in the labeled phase and its oldest metadata age exceeds:

| Phases | Threshold before the two-minute pending duration |
| --- | --- |
| `provisioning`, `committing`, `pr_created` | Strictly greater than 600 seconds |
| `analyzing`, `implementing`, `verifying` | Strictly greater than 1,800 seconds |

Use authorized work detail/events, Worker heartbeat/quarantine, lease validity and actual host observations to distinguish a healthy long operation from loss of progress. The values are starting thresholds, not a task timeout or SLO; change rule fixtures with any threshold adjustment. Human waits are deliberate and must not be bypassed. Do not cancel active work, release leases or restart a VM based only on this alert. Those operations need their own audited, host-verified recovery workflow.

<a id="delivery"></a>

### `KelpieDeliveryJobStalled`

A `pending`, `retry`, `running` or `unknown` delivery job has metadata age strictly greater than 600 seconds. Inspect authorized work state, [approval-linked audit](delivery-audit.md), [safe failure codes](delivery-failure-safety.md), bundle integrity, startup recovery and SCM access. Check unknown states against the supported API version without exporting raw values. `completed` and `failed` history does not fire this alert; observed failures have a [separate counter-based alert](monitoring-alerts.md#delivery-failures).

Do not manufacture approvals, edit job states, run a second API delivery process, retry quarantined work or bypass resource gates. Synthetic state changes in acceptance tests are not a product recovery API. An alert does not prove actual SCM publication or real VM health.

## Verification and rollback

Run `make test-api` and `make lint`. `test_runtime_health.py` covers SQLite and, with `KELPIE_TEST_POSTGRES_URL` pointing at a dedicated test database, PostgreSQL; five database cases skip without that URL. Existing required `Python` CI uses its PostgreSQL service. `test_execution_health_http.py` runs real migrated SQLite/Uvicorn/HTTP through delivery-table query failure, retained values and recovery without API restart. It preserves the original ten-second observation cadence (about 31 seconds), checks responsive `/metrics`, and verifies no Worker, lease or audit mutation.

`make test-monitoring PROMTOOL=/path/to/promtool` now runs both fixture files: 44 scenarios and 174 evaluation-time assertions across 14 rules (12 alerts, two recording rules). Added coverage includes all phase thresholds, human/terminal exclusions, pending/firing/recovery, missing/invalid/duplicate states and continued old alerts during partial collection loss. Required `Go` CI reuses the checksum-verified cached Prometheus binary and synthetic clock; no extra CI job, credential or timeout increase is needed.

Rollback the new API metrics and dependent execution rules together, validate configuration and fixtures, then perform an authorized restart/reload. Keeping new rules with an older API intentionally warns about missing observations. No data migration, audit rewrite, TSDB deletion or resource operation is required.

## Actual runtime verification — 2026-09-09

Verification covers implementation `72186d3`, rules `1e61809` with duplicate-detection regression fix `440e208`, and actual HTTP regression `3d5e689`. At `de0440c`, a normal merge of PR #41's isolated fault-injection deadline fix, `make test` using an owned temporary PostgreSQL database passed 989 API tests (193.01 seconds, no skips), six Runner tests, Worker/Gateway Go tests, and 91 Web tests plus type checking. `make lint` and the 44 rule scenarios above also passed. Product timeouts were not changed to accelerate verification.

The run used migrated disposable SQLite, actual Uvicorn/API, a loopback scrape proxy, and Prometheus 3.14.0 with its checksum reverified. The ten-second observation cadence, 15-second scrape, 30-second evaluation and two-minute pending duration were not shortened. Two `committing` works, one `implementing` work, and one each of `pending`/`running` delivery jobs were synthetic rows inserted only after automatic startup recovery completed; no real SCM publication or VM execution occurred.

An earlier 2026-09-06 run of rules `1e61809` dropped only the delivery `pending` metric and verified unavailable observation progressing from pending to firing, suppression of new stall alerts, and continued three existing warnings. That is separate from verification of the final duplicate-detection fix.

Final-run observations, in UTC:

| Time | Verified state |
| --- | --- |
| Pending since 00:41:23; checked at 00:43:34 | All four instances across two execution phases and two delivery states firing |
| Pending since 00:47:53; checked at 00:49:55 | A NaN duplicate with the valid `pending` copy still present removed the extended gate and fired unavailable observation; new stall alerts were suppressed while the existing three warnings remained |
| 00:50:23 evaluation → checked after 00:50:53 evaluation | Removing the duplicate and recovering synthetic states restored gate 1. The first evaluation briefly made stalls pending using the previous snapshot; the next cleared all three new alert types, leaving only existing Worker/lease/queue warnings firing |

Recovery was a test mutation of owned synthetic works to approval wait and deliveries to completed. Worker, lease and audit rows were identical before/after; the expired active lease remained. This does not claim overall system health, resource reclamation or actual VM recovery.

Expanded rules in the Orca browser to inspect Korean/English summaries, runbooks and two-minute waits, then inspected refreshed states and actual desktop screenshots through computer-use. All 36 captured browser requests returned 200, with zero external requests and console messages. Screenshots containing local paths and other project information were not published. OS focus was unsupported, so native keyboard/mouse input was not verified. External notification receipt, production deployment and real Worker/KVM verification were not performed.

Stopped the owned API/proxy/Prometheus, closed their browser/terminal tabs, and verified no listeners on loopback ports 18570, 18571 or 19570. Fixture lifecycle cleaned the synthetic database; no stop/change commands were sent to existing user terminals or services.
