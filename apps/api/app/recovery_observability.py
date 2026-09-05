"""Process-local startup recovery metrics; never a health signal for the whole queue."""

import time
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Enum, Gauge


class DeliveryRecoveryMetrics:
    def __init__(
        self, registry: CollectorRegistry, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._started: float | None = None
        self._finished: float | None = None
        self._state = Enum(
            "kelpie_delivery_startup_recovery_state",
            "Startup recovery phase in this API process; not overall delivery health.",
            states=("not_started", "waiting_for_database", "running", "retrying",
                    "completed", "cancelled"),
            registry=registry,
        )
        duration = Gauge(
            "kelpie_delivery_startup_recovery_duration_seconds",
            "Elapsed startup recovery time including waits; frozen after completion/cancellation.",
            registry=registry,
        )
        duration.set_function(self._elapsed)
        self._checks = Counter(
            "kelpie_delivery_startup_recovery_checks_total",
            "Finished readiness/recovery iterations, not individual delivery results.",
            ("outcome",), registry=registry,
        )
        for outcome in ("database_unready", "completed", "error"):
            self._checks.labels(outcome=outcome)

    def _elapsed(self) -> float:
        if self._started is None:
            return 0
        end = self._finished if self._finished is not None else self._clock()
        return max(0, end - self._started)

    def start(self) -> None:
        self._started, self._finished = self._clock(), None
        self._state.state("waiting_for_database")

    def database_unready(self) -> None:
        self._checks.labels(outcome="database_unready").inc()
        self._state.state("waiting_for_database")

    def running(self) -> None:
        self._state.state("running")

    def error(self) -> None:
        self._checks.labels(outcome="error").inc()
        self._state.state("retrying")

    def complete(self) -> None:
        self._checks.labels(outcome="completed").inc()
        self._finished = self._clock()
        self._state.state("completed")

    def cancel(self) -> None:
        if self._finished is None:
            self._finished = self._clock()
        self._state.state("cancelled")
