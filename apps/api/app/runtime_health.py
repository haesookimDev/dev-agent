"""Read-only, bounded-cardinality snapshots; never release leases or restart work."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import case, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ResourceLease, WorkerHost, WorkerState, WorkItem, WorkStatus

WORKER_STATES = ("online", "draining", "offline", "quarantined")
SNAPSHOT_MAX_AGE_SECONDS = 30


@dataclass(frozen=True)
class RuntimeSnapshot:
    observed_at: datetime
    workers: tuple[int, int, int, int]
    active_leases: int
    expired_leases: int
    queued_work: int
    oldest_queued_seconds: float


async def read_runtime_snapshot(
    session: AsyncSession, *, now: datetime, worker_offline_seconds: int,
) -> RuntimeSnapshot:
    """One aggregate statement gives a coherent PostgreSQL statement snapshot.

    No identifiers, credentials, payloads, row locks, or writes are requested.
    The caller bounds checkout, query execution, and connection cleanup together.
    """
    worker_state = case(
        (WorkerHost.quarantined_at.is_not(None), "quarantined"),
        ((WorkerHost.state == WorkerState.OFFLINE) | (
            WorkerHost.last_seen_at <= now - timedelta(seconds=worker_offline_seconds)
        ), "offline"),
        (WorkerHost.state == WorkerState.DRAINING, "draining"),
        else_="online",
    )
    workers = select(*(
        func.count().filter(worker_state == state).label(state) for state in WORKER_STATES
    )).select_from(WorkerHost).subquery()
    leases = select(
        func.count().filter(ResourceLease.expires_at >= now).label("active_leases"),
        func.count().filter(ResourceLease.expires_at < now).label("expired_leases"),
    ).where(ResourceLease.state == "active").subquery()
    queue = select(
        func.count().label("queued_work"), func.min(WorkItem.created_at).label("oldest"),
    ).where(WorkItem.status == WorkStatus.QUEUED).subquery()
    row = (await session.execute(select(workers, leases, queue).select_from(
        workers.join(leases, true()).join(queue, true()),
    ))).one()
    oldest = row.oldest
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)  # SQLite stores UTC without a timezone.
    return RuntimeSnapshot(
        observed_at=now,
        workers=tuple(getattr(row, state) for state in WORKER_STATES),
        active_leases=row.active_leases, expired_leases=row.expired_leases,
        queued_work=row.queued_work,
        oldest_queued_seconds=max(0, (now - oldest).total_seconds()) if oldest else 0,
    )


@dataclass(frozen=True)
class _Observation:
    snapshot: RuntimeSnapshot | None = None
    monotonic_at: float = 0
    successful: bool = False


class RuntimeHealthMetrics:
    """Publish an immutable observation atomically; scrapes never access the DB.

    Old data remains available for diagnosis, but availability becomes zero on
    failure, stop, or monotonic age > 30s. Before the first success, data is absent
    (not a fabricated empty fleet) and snapshot age is NaN.
    """

    def __init__(
        self, registry: CollectorRegistry, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._observation = _Observation()
        registry.register(self)

    def reset(self) -> None:
        self._observation = _Observation()

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        self._observation = _Observation(snapshot, self._clock(), True)

    def unavailable(self) -> None:
        self._observation = replace(self._observation, successful=False)

    def collect(self) -> Iterator[GaugeMetricFamily]:
        observation = self._observation
        snapshot = observation.snapshot
        age = max(0, self._clock() - observation.monotonic_at) if snapshot else float("nan")
        yield GaugeMetricFamily(
            "kelpie_runtime_snapshot_available",
            "Last runtime observation succeeded and is no more than 30 seconds old.",
            value=int(observation.successful and age <= SNAPSHOT_MAX_AGE_SECONDS),
        )
        yield GaugeMetricFamily(
            "kelpie_runtime_snapshot_age_seconds",
            "Monotonic age of last successful observation; NaN before first success.", value=age,
        )
        yield GaugeMetricFamily(
            "kelpie_runtime_snapshot_timestamp_seconds",
            "Unix time of last successful runtime observation; zero before first success.",
            value=snapshot.observed_at.timestamp() if snapshot else 0,
        )
        if snapshot is None:
            return
        workers = GaugeMetricFamily(
            "kelpie_runtime_workers", "Registered workers by observed state.", labels=["state"],
        )
        for state, count in zip(WORKER_STATES, snapshot.workers, strict=True):
            workers.add_metric([state], count)
        yield workers
        leases = GaugeMetricFamily(
            "kelpie_runtime_leases",
            "Active lease records by expiry, excluding released/quarantined.",
            labels=["state"],
        )
        leases.add_metric(["active"], snapshot.active_leases)
        leases.add_metric(["expired"], snapshot.expired_leases)
        yield leases
        yield GaugeMetricFamily(
            "kelpie_runtime_queued_work", "Work items currently queued, not waiting for humans.",
            value=snapshot.queued_work,
        )
        yield GaugeMetricFamily(
            "kelpie_runtime_queue_oldest_age_seconds",
            "Age since creation of the oldest currently queued work at observation time.",
            value=snapshot.oldest_queued_seconds,
        )
