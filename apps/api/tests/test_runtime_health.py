import math
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine

from app.models import (
    Base,
    Organization,
    ResourceLease,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkSource,
    WorkStatus,
)
from app.runtime_health import RuntimeHealthMetrics, RuntimeSnapshot, read_runtime_snapshot

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(tmp_path, request):
    if request.param == "postgres":
        url = os.environ.get("KELPIE_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("dedicated PostgreSQL test URL not set")
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    schema = f"runtime_test_{uuid.uuid4().hex}"
                    await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                    await connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                    await connection.run_sync(Base.metadata.create_all)
                    factory = async_sessionmaker(
                        connection, expire_on_commit=False,
                        join_transaction_mode="create_savepoint",
                    )
                    async with factory() as session:
                        session.add(Organization(id="legacy"))
                        await session.commit()
                    yield factory
                finally:
                    await transaction.rollback()  # Only this test's schema and rows.
        finally:
            await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def worker(name, *, age=0, state=WorkerState.ONLINE, quarantined=False):
    return WorkerHost(
        id=name, name=name, state=state, last_seen_at=NOW - timedelta(seconds=age),
        quarantined_at=NOW if quarantined else None, cpu_total=8, cpu_available=8,
        memory_mb_total=8192, memory_mb_available=8192, disk_gb_available=100,
    )


def work(name, *, age=0, status=WorkStatus.QUEUED):
    return WorkItem(
        id=name, title=name, requirement="private task body", repository="private/repository",
        source=WorkSource.WEB, status=status, created_at=NOW - timedelta(seconds=age),
    )


async def seed_snapshot(sessions):
    async with sessions() as session:
        session.add_all([
            worker("online", age=44), worker("future", age=-10),
            worker("draining", state=WorkerState.DRAINING),
            worker("offline", state=WorkerState.OFFLINE), worker("expired", age=45),
            worker("draining-expired", age=46, state=WorkerState.DRAINING),
            worker("quarantined", age=100, quarantined=True),
            work("old", age=600), work("new", age=100),
            work("awaiting-human", age=10000, status=WorkStatus.AWAITING_APPROVAL),
            work("cancelled", age=10000, status=WorkStatus.CANCELLED),
        ])
        await session.flush()
        for identity, state, age in [
            ("old", "active", 1), ("new", "active", 0),
            ("awaiting-human", "released", 100), ("cancelled", "quarantined", 100),
        ]:
            session.add(ResourceLease(
                work_item_id=identity, worker_id="online", token_hash=identity, state=state,
                expires_at=NOW - timedelta(seconds=age),
            ))
        await session.commit()


async def test_empty_database_is_an_explicit_successful_empty_snapshot(sessions):
    async with sessions() as session:
        value = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
    assert value == RuntimeSnapshot(NOW, (0, 0, 0, 0), 0, 0, 0, 0)


async def test_one_read_only_statement_counts_states_and_exact_boundaries(sessions):
    await seed_snapshot(sessions)
    statements = []
    bind = sessions.kw["bind"]
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with sessions() as session:
            value = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert value == RuntimeSnapshot(NOW, (2, 1, 3, 1), 1, 1, 2, 600)
    queries = [statement for statement in statements if "SAVEPOINT" not in statement]
    assert len(queries) == 1 and queries[0].startswith("SELECT")
    assert "FOR UPDATE" not in queries[0]
    for private in ("token_hash", "requirement", "repository", "labels"):
        assert private not in queries[0]
    async with sessions() as session:
        assert (await session.get(WorkerHost, "expired")).state == WorkerState.ONLINE
        assert list(await session.scalars(select(ResourceLease.state))).count("active") == 2


async def test_future_queue_age_is_clamped_and_human_waits_are_excluded(sessions):
    async with sessions() as session:
        session.add(work("future", age=-10))
        session.add(work("input", age=1000, status=WorkStatus.AWAITING_INPUT))
        await session.commit()
        value = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
    assert value.queued_work == 1 and value.oldest_queued_seconds == 0


def values(registry):
    return {(sample.name.removeprefix("kelpie_runtime_"), tuple(sample.labels.items())):
            sample.value for family in registry.collect() for sample in family.samples}


def test_initial_failure_staleness_recovery_and_restart_never_fabricate_health():
    clock = SimpleNamespace(now=100)
    registry = CollectorRegistry()
    metrics = RuntimeHealthMetrics(registry, clock=lambda: clock.now)
    initial = values(registry)
    assert initial["snapshot_available", ()] == 0
    assert math.isnan(initial["snapshot_age_seconds", ()])
    assert initial["snapshot_timestamp_seconds", ()] == 0
    assert len(initial) == 3
    metrics.unavailable()
    assert len(values(registry)) == 3
    snapshot = RuntimeSnapshot(NOW, (1, 0, 2, 0), 1, 2, 3, 600)
    metrics.publish(snapshot)
    assert values(registry)["snapshot_available", ()] == 1
    clock.now = 130
    assert values(registry)["snapshot_available", ()] == 1
    clock.now = 131
    assert values(registry)["snapshot_available", ()] == 0
    assert values(registry)["queued_work", ()] == 3
    metrics.publish(snapshot)
    assert values(registry)["snapshot_age_seconds", ()] == 0
    metrics.unavailable()
    assert values(registry)["snapshot_available", ()] == 0
    assert values(registry)["leases", (("state", "expired"),)] == 2
    metrics.publish(RuntimeSnapshot(NOW, (0, 0, 0, 0), 0, 0, 0, 0))
    assert values(registry)["snapshot_available", ()] == 1
    assert values(registry)["queued_work", ()] == 0
    metrics.reset()
    assert len(values(registry)) == 3 and values(registry)["snapshot_available", ()] == 0


def test_scrape_is_coherent_even_if_a_snapshot_is_replaced_mid_collection():
    registry = CollectorRegistry()
    metrics = RuntimeHealthMetrics(registry)
    metrics.publish(RuntimeSnapshot(NOW, (1, 2, 3, 4), 5, 6, 7, 8))
    scrape = metrics.collect()
    assert next(scrape).samples[0].value == 1
    metrics.publish(RuntimeSnapshot(NOW, (9, 9, 9, 9), 9, 9, 9, 9))
    old_queue = next(family for family in scrape if family.name == "kelpie_runtime_queued_work")
    assert old_queue.samples[0].value == 7
    assert values(registry)["queued_work", ()] == 9
    exposed = generate_latest(registry).decode()
    assert 'state="offline"' in exposed and 'state="quarantined"' in exposed
    assert all(set(sample.labels) <= {"state"}
               for family in registry.collect() for sample in family.samples)
