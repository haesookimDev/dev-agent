import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest

from app import main, observability
from app.db import SchemaReadiness, SchemaState
from app.recovery_observability import DeliveryRecoveryMetrics

PREFIX = "kelpie_delivery_startup_recovery_"


@pytest.fixture(autouse=True)
def isolate_unrelated_runtime_monitor(monkeypatch):
    monkeypatch.setattr(main, "monitor_runtime_health", AsyncMock())


@pytest.fixture
def metrics(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    registry = CollectorRegistry()
    value = DeliveryRecoveryMetrics(registry, clock=lambda: clock.now)
    monkeypatch.setattr(main, "DELIVERY_RECOVERY", value)
    return SimpleNamespace(value=value, registry=registry, clock=clock)


def samples(metrics):
    return [sample for family in metrics.registry.collect() for sample in family.samples]


def phase(metrics):
    values = [sample for sample in samples(metrics) if sample.name == PREFIX + "state"]
    assert len(values) == 6
    assert all(sample.value in (0, 1) for sample in values)
    assert sum(sample.value for sample in values) == 1
    return next(sample.labels[PREFIX + "state"] for sample in values if sample.value == 1)


def count(metrics, outcome):
    return next(sample.value for sample in samples(metrics)
                if sample.name == PREFIX + "checks_total" and sample.labels == {"outcome": outcome})


def duration(metrics):
    return next(sample.value for sample in samples(metrics)
                if sample.name == PREFIX + "duration_seconds")


def test_metrics_start_unknown_and_use_only_fixed_labels(metrics):
    assert phase(metrics) == "not_started"
    assert duration(metrics) == 0
    assert [count(metrics, outcome) for outcome in ("database_unready", "completed", "error")] == [
        0, 0, 0,
    ]
    assert all(set(sample.labels) <= {PREFIX + "state", "outcome"} for sample in samples(metrics))


def test_elapsed_includes_waits_and_freezes_at_completion(metrics):
    metrics.value.start()
    metrics.clock.now = 105
    metrics.value.database_unready()
    metrics.clock.now = 111
    metrics.value.running()
    assert phase(metrics) == "running" and duration(metrics) == 11
    metrics.value.error()
    metrics.clock.now = 120
    metrics.value.running()
    metrics.value.complete()
    metrics.clock.now = 200
    assert phase(metrics) == "completed" and duration(metrics) == 20
    assert [count(metrics, outcome) for outcome in ("database_unready", "completed", "error")] == [
        1, 1, 1,
    ]
    metrics.value.start()
    assert phase(metrics) == "waiting_for_database" and duration(metrics) == 0
    assert count(metrics, "completed") == 1  # Counters span this process, not one lifespan.


async def test_recovery_reports_unready_running_error_then_completion(metrics, monkeypatch):
    release_check, release_error, release_job = (asyncio.Event() for _ in range(3))
    unready, retrying, running = (asyncio.Event() for _ in range(3))
    checks, deliveries = 0, 0

    async def ready():
        nonlocal checks
        checks += 1
        if checks == 1:
            return SchemaReadiness(SchemaState.UNREACHABLE)
        unready.set()
        await release_check.wait()
        if checks >= 3:
            retrying.set()
            await release_error.wait()
        return SchemaReadiness(SchemaState.CURRENT)

    async def resume():
        nonlocal deliveries
        deliveries += 1
        if deliveries == 1:
            raise RuntimeError("synthetic-private-recovery-error")
        running.set()
        await release_job.wait()

    monkeypatch.setattr(main, "get_schema_readiness", ready)
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    monkeypatch.setattr(main, "DELIVERY_RECOVERY_RETRY_SECONDS", 0.01)
    metrics.value.start()
    task = asyncio.create_task(main.recover_startup_deliveries())
    try:
        await asyncio.wait_for(unready.wait(), 1)
        assert phase(metrics) == "waiting_for_database"
        assert count(metrics, "database_unready") == 1
        release_check.set()
        await asyncio.wait_for(retrying.wait(), 1)
        assert phase(metrics) == "retrying"
        assert count(metrics, "error") == 1
        release_error.set()
        await asyncio.wait_for(running.wait(), 1)
        assert phase(metrics) == "running"
        assert count(metrics, "completed") == 0
        release_job.set()
        await asyncio.wait_for(task, 1)
        assert phase(metrics) == "completed" and count(metrics, "completed") == 1
        assert "synthetic-private" not in generate_latest(metrics.registry).decode()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("where", ["schema", "delivery", "backoff"])
async def test_cancellation_is_not_an_error_or_completed_scan(metrics, monkeypatch, where):
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    async def ready():
        if where == "schema":
            await block()
        if where == "backoff":
            entered.set()
            return SchemaReadiness(SchemaState.OUTDATED)
        return SchemaReadiness(SchemaState.CURRENT)

    monkeypatch.setattr(main, "get_schema_readiness", ready)
    monkeypatch.setattr(main, "resume_pending_deliveries", block)
    metrics.value.start()
    task = asyncio.create_task(main.recover_startup_deliveries())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        metrics.clock.now = 103
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        metrics.clock.now = 999
        assert phase(metrics) == "cancelled" and duration(metrics) == 3
        assert count(metrics, "completed") == count(metrics, "error") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("previous_completed", [False, True])
async def test_immediate_lifespan_shutdown_marks_never_scheduled_recovery_cancelled(
    metrics, monkeypatch, previous_completed,
):
    if previous_completed:
        metrics.value.start()
        metrics.clock.now = 120
        metrics.value.complete()
        metrics.clock.now = 200
    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    monkeypatch.setattr(main, "get_schema_readiness", AsyncMock(
        return_value=SchemaReadiness(SchemaState.CURRENT),
    ))
    resume = AsyncMock()
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    async with main.lifespan(main.app):
        pass  # No loop step is available for the child before shutdown.
    resume.assert_not_awaited()
    assert phase(metrics) == "cancelled" and duration(metrics) == 0


async def test_metrics_route_exposes_recovery_without_database_access(client, monkeypatch):
    observability.DELIVERY_RECOVERY.start()
    observability.DELIVERY_RECOVERY.database_unready()
    unavailable = AsyncMock(side_effect=RuntimeError("must not inspect DB while scraping"))
    monkeypatch.setattr(main, "get_schema_readiness", unavailable)
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert f'{PREFIX}state{{{PREFIX}state="waiting_for_database"}} 1.0' in response.text
    unavailable.assert_not_awaited()


async def test_successful_lifespan_retains_completed_phase_and_frozen_duration(
    metrics, monkeypatch,
):
    finished = asyncio.Event()

    async def resume():
        metrics.clock.now = 112
        finished.set()

    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    monkeypatch.setattr(main, "get_schema_readiness", AsyncMock(
        return_value=SchemaReadiness(SchemaState.CURRENT),
    ))
    monkeypatch.setattr(main, "resume_pending_deliveries", resume)
    async with main.lifespan(main.app):
        await asyncio.wait_for(finished.wait(), 1)
        assert phase(metrics) == "completed"
        metrics.clock.now = 1000
    assert phase(metrics) == "completed" and duration(metrics) == 12
    assert count(metrics, "completed") == 1
