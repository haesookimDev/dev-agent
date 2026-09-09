# Bounded API parallel checks

[한국어](../ko/ci-partitions.md) | English · [Development guide](development.md)

## Purpose and execution structure

After PR #50 merged, the Python job in [main CI](https://github.com/haesookimDev/dev-agent/actions/runs/34316776712) exceeded its eight-minute limit after 90% of API checks. One retry of the same SHA passed in 6m 8s; the original failure remains recorded. Split the complete API collection into two partitions without increasing timeouts or removing regressions.

- `API (1/2)` and `API (2/2)` collect the same complete suite on separate runners and select their partition using SHA-256 of each file's relative path. Parametrized cases, fixtures within a file and collection order stay together; new tests are automatically included.
- The existing required `Python` job runs after both partitions finish. `always()` prevents failed dependencies from turning it into a misleading skip, and its first shell step accepts only `needs.api.result == success`. Failure, cancellation, skipping and missing results are not success.
- `Python` then runs Runner/Ruff and every existing PostgreSQL 17 migration, Worker isolation, audit, retention, race, observation, SSE and restore check. The partition option exists only on the API step; it does not split or omit database checks.
- `Go` and `Web` commands, actual Chromium coverage and retained evidence remain unchanged. Keep the existing required names `Python`, `Go`, `Web` and inspect both API checks too.

All five jobs retain eight-minute limits. Python 3.12 and Ubuntu 24.04 remain fixed; the two partitions do not add a version matrix. API uses `fail-fast: false` to collect both results after one fails, without hiding failures through `continue-on-error`. Job setup overhead may grow, and total CI duration still varies with runner performance and queue time.

## Local verification and interpretation

Default commands still execute the complete suite.

```sh
make test-api
make lint
```

When changing partition behavior, run both commands below as well. They can run concurrently in separate processes.

```sh
PYTEST_ADDOPTS='--api-partition=1 --durations=10' make test-api
PYTEST_ADDOPTS='--api-partition=2 --durations=10' make test-api
```

`deselected` means assigned to the other partition. Compare both results' passed/failed/skipped totals with the complete collection; one successful partition is not a successful suite. API skips caused by missing PostgreSQL settings still require the existing dedicated database steps. Do not use arbitrary `-k` filters, path lists, test deletion or global selection options to meet timing targets.

`test_ci_partition.py` checks default full collection, a complete disjoint union, cases staying in their files, checkout/order independence and actual pytest failure exit codes and invalid-option rejection. `test_ci_workflow.py` verifies the fixed matrix, required dependency gate and failure rejection by its actual shell command. No dependency is added.

## Local verification, 2026-09-09

- At `f6f7020`, `make test` and `make lint` passed: API 1,108 passed/98 skipped in 182.71s; Runner 45; Worker/Gateway; Web 125 and type checking.
- The workflow regression failed seven checks before the change, then passed all 16 checks together with partition coverage.
- At `0f9d1fe`, the final complete collection of 1,213 equals partitions of 504 and 709, with zero omissions or overlap. Concurrent execution produced 470 passed/34 skipped in 58.38s and 645 passed/64 skipped in 124.32s; `make lint` also passed. Local timing does not guarantee GitHub duration.
- Record actual GitHub head, final status, duration, subsequent PostgreSQL checks and post-merge main results in the PR. This CI-only change has no new UI/native-input behavior to inspect and does not replace actual KVM/Preview acceptance.

To roll back, revert the workflow connection together with its coupled workflow regression and restore the full API command in `Python`. Retaining only the partition tool is safe: omitting the option still runs everything. Production data, authorization and deployment settings are unchanged; pinned action SHAs, read-only tokens and existing caches remain.

The gate follows GitHub's [job dependencies and `always()`](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs), [matrix failure handling](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations) and [`needs` results](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#needs-context).
