import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from prometheus_client import CollectorRegistry
from test_runtime_health import NOW, values

from app import main, observability, runtime_monitor
from app.config import Settings
from app.db import SchemaReadiness, SchemaState
from app.runtime_health import RuntimeHealthMetrics, RuntimeSnapshot

SNAPSHOT = RuntimeSnapshot(NOW, (1, 0, 0, 0), 2, 3, 4, 500)


@pytest.fixture
def monitor(monkeypatch):
    registry = CollectorRegistry()
    metrics = RuntimeHealthMetrics(registry)
    monkeypatch.setattr(main, "RUNTIME_HEALTH", metrics)
    monkeypatch.setattr(main, "configure_observability", lambda _: None)
    ready = AsyncMock(return_value=SchemaReadiness(SchemaState.CURRENT))
    monkeypatch.setattr(main, "get_schema_readiness", ready)
    monkeypatch.setattr(runtime_monitor, "get_schema_readiness", ready)
    monkeypatch.setattr(runtime_monitor, "OBSERVATION_INTERVAL_SECONDS", 0.01)
    read = AsyncMock(return_value=SNAPSHOT)
    monkeypatch.setattr(runtime_monitor, "read_runtime_snapshot", read)
    entered, closed = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def session():
        entered.set()
        try:
            yield object()
        finally:
            closed.set()

    monkeypatch.setattr(runtime_monitor, "SessionLocal", session)
    return SimpleNamespace(
        metrics=metrics, registry=registry, read=read, ready=ready, entered=entered, closed=closed,
    )


async def until(predicate):
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    handle = None

    def check():
        nonlocal handle
        if predicate():
            changed.set()
        else:
            handle = loop.call_later(0.001, check)

    check()
    try:
        await asyncio.wait_for(changed.wait(), 1)
    finally:
        if handle:
            handle.cancel()


async def test_unready_error_then_recovery_retains_only_complete_safe_observations(
    monitor, monkeypatch, caplog,
):
    monkeypatch.setattr(runtime_monitor.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=runtime_monitor.__name__)
    monitor.ready.return_value = SchemaReadiness(SchemaState.OUTDATED)
    task = asyncio.create_task(runtime_monitor.monitor_runtime_health(monitor.metrics, Settings()))
    try:
        await until(lambda: monitor.ready.await_count > 0)
        monitor.read.assert_not_awaited()
        assert values(monitor.registry)["snapshot_available", ()] == 0
        assert len(values(monitor.registry)) == 3
        monitor.ready.return_value = SchemaReadiness(SchemaState.CURRENT)
        await until(lambda: values(monitor.registry)["snapshot_available", ()] == 1)
        assert monitor.closed.is_set()
        monitor.read.side_effect = RuntimeError("private-dsn-and-query-parameters")
        await until(lambda: values(monitor.registry)["snapshot_available", ()] == 0)
        assert values(monitor.registry)["queued_work", ()] == 4
        assert "runtime health observation failed; retrying" in caplog.text
        assert "private-dsn" not in caplog.text
        monitor.read.side_effect = None
        monitor.read.return_value = RuntimeSnapshot(NOW, (0, 0, 0, 0), 0, 0, 0, 0)
        await until(lambda: values(monitor.registry)["snapshot_available", ()] == 1)
        assert values(monitor.registry)["queued_work", ()] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert values(monitor.registry)["snapshot_available", ()] == 0


@pytest.mark.parametrize("where", ["readiness", "checkout", "query", "cleanup"])
async def test_timeout_includes_every_stage_without_partial_publish(
    monitor, monkeypatch, where,
):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def hang(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    if where == "readiness":
        monitor.ready.side_effect = hang
    elif where == "query":
        monitor.read.side_effect = hang
    else:
        @asynccontextmanager
        async def session():
            if where == "checkout":
                await hang()
            yield object()
            if where == "cleanup":
                await hang()
        monkeypatch.setattr(runtime_monitor, "SessionLocal", session)
    monkeypatch.setattr(runtime_monitor, "OBSERVATION_TIMEOUT_SECONDS", 0.02)
    # Leave enough backoff to inspect the first timed-out attempt deterministically.
    monkeypatch.setattr(runtime_monitor, "OBSERVATION_INTERVAL_SECONDS", 10)
    task = asyncio.create_task(runtime_monitor.monitor_runtime_health(monitor.metrics, Settings()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(stopped.wait(), 1)
        assert values(monitor.registry)["snapshot_available", ()] == 0
        assert len(values(monitor.registry)) == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_monitor_continues_while_startup_recovery_is_blocked_and_shutdown_joins_it(
    monitor, monkeypatch,
):
    recovering, stopped = asyncio.Event(), asyncio.Event()

    async def recovery():
        recovering.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(main, "resume_pending_deliveries", recovery)
    baseline = asyncio.all_tasks()
    async with main.lifespan(main.app):
        await asyncio.wait_for(recovering.wait(), 1)
        await until(lambda: monitor.read.await_count >= 2)
        assert values(monitor.registry)["snapshot_available", ()] == 1
        assert monitor.read.call_args.kwargs["worker_offline_seconds"] == 45
    assert stopped.is_set() and monitor.closed.is_set()
    assert not asyncio.all_tasks() - baseline
    assert values(monitor.registry)["snapshot_available", ()] == 0


async def test_immediate_shutdown_and_repeat_lifespans_reset_previous_observation(monitor):
    for _ in range(2):
        monitor.metrics.publish(SNAPSHOT)
        async with main.lifespan(main.app):
            assert values(monitor.registry)["snapshot_available", ()] == 0
            assert len(values(monitor.registry)) == 3
        assert values(monitor.registry)["snapshot_available", ()] == 0
    monitor.read.assert_not_awaited()


async def test_cancellation_during_query_cleans_up_session_and_does_not_publish(monitor):
    entered = asyncio.Event()

    async def read(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monitor.read.side_effect = read
    task = asyncio.create_task(runtime_monitor.monitor_runtime_health(monitor.metrics, Settings()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert monitor.closed.is_set() and len(values(monitor.registry)) == 3


async def test_metrics_scrape_remains_fast_and_database_independent(client, monitor, monkeypatch):
    monkeypatch.setattr(observability, "REGISTRY", monitor.registry)
    monitor.read.side_effect = RuntimeError("must not query on scrape")
    async with asyncio.timeout(0.5):
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "kelpie_runtime_snapshot_available 0.0" in response.text
    assert "kelpie_runtime_queued_work" not in response.text
    monitor.read.assert_not_awaited()
