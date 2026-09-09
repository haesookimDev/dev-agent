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
    DeliveryJob,
    Organization,
    ResourceLease,
    WorkerHost,
    WorkerState,
    WorkItem,
    WorkSource,
    WorkStatus,
)
from app.runtime_health import (
    DELIVERY_STATES,
    EXECUTION_STATES,
    RuntimeHealthMetrics,
    RuntimeSnapshot,
    StateSnapshot,
    read_runtime_snapshot,
)

NOW = datetime(2026, 9, 6, tzinfo=UTC)
EMPTY_EXECUTION = tuple(StateSnapshot(0, 0) for _ in EXECUTION_STATES)
EMPTY_DELIVERY = tuple(StateSnapshot(0, 0) for _ in DELIVERY_STATES)


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
    assert value == RuntimeSnapshot(NOW, (0, 0, 0, 0), 0, 0, 0, 0,
                                    EMPTY_EXECUTION, EMPTY_DELIVERY)


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
    assert value == RuntimeSnapshot(NOW, (2, 1, 3, 1), 1, 1, 2, 600,
                                    EMPTY_EXECUTION, EMPTY_DELIVERY)
    queries = [statement for statement in statements if "SAVEPOINT" not in statement]
    assert len(queries) == 1 and queries[0].startswith("SELECT")
    assert "FOR UPDATE" not in queries[0]
    for private in ("token_hash", "requirement", "repository", "labels", "error"):
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
    snapshot = RuntimeSnapshot(NOW, (1, 0, 2, 0), 1, 2, 3, 600, EMPTY_EXECUTION, EMPTY_DELIVERY)
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
    metrics.publish(RuntimeSnapshot(NOW, (0, 0, 0, 0), 0, 0, 0, 0, EMPTY_EXECUTION, EMPTY_DELIVERY))
    assert values(registry)["snapshot_available", ()] == 1
    assert values(registry)["queued_work", ()] == 0
    metrics.reset()
    assert len(values(registry)) == 3 and values(registry)["snapshot_available", ()] == 0


def test_scrape_is_coherent_even_if_a_snapshot_is_replaced_mid_collection():
    registry = CollectorRegistry()
    metrics = RuntimeHealthMetrics(registry)
    execution = tuple(StateSnapshot(2, 100) for _ in EXECUTION_STATES)
    metrics.publish(RuntimeSnapshot(NOW, (1, 2, 3, 4), 5, 6, 7, 8, execution, EMPTY_DELIVERY))
    scrape = metrics.collect()
    assert next(scrape).samples[0].value == 1
    metrics.publish(RuntimeSnapshot(NOW, (9, 9, 9, 9), 9, 9, 9, 9, EMPTY_EXECUTION, EMPTY_DELIVERY))
    old_queue = next(family for family in scrape if family.name == "kelpie_runtime_queued_work")
    assert old_queue.samples[0].value == 7
    old_execution = next(family for family in scrape
                         if family.name == "kelpie_runtime_execution_work")
    assert all(sample.value == 2 for sample in old_execution.samples)
    assert values(registry)["queued_work", ()] == 9
    exposed = generate_latest(registry).decode()
    assert 'state="offline"' in exposed and 'state="quarantined"' in exposed
    assert all(set(sample.labels) <= {"state"}
               for family in registry.collect() for sample in family.samples)


async def test_execution_and_delivery_observations_cover_fixed_states_without_human_waits(sessions):
    phases = (WorkStatus.PROVISIONING, WorkStatus.ANALYZING, WorkStatus.IMPLEMENTING,
              WorkStatus.VERIFYING, WorkStatus.COMMITTING, WorkStatus.PR_CREATED)
    jobs = ("pending", "retry", "running", "completed", "failed", "private-unknown-state")
    async with sessions() as session:
        for index, phase in enumerate(phases):
            for suffix, age in (("old", 600 + index * 30), ("new", 10)):
                row = work(f"{phase.value}-{suffix}", status=phase)
                row.updated_at = NOW - timedelta(seconds=age)
                session.add(row)
        for phase in set(WorkStatus) - set(phases):
            row = work(phase.value, status=phase)
            row.updated_at = NOW - timedelta(days=10)
            session.add(row)
        for index, state in enumerate(jobs):
            row = work(f"delivery-{index}", status=WorkStatus.COMPLETED)
            session.add(row)
            await session.flush()
            session.add(DeliveryJob(work_item_id=row.id, state=state,
                error="private upstream message", updated_at=NOW - timedelta(seconds=100 + index)))
        await session.commit()
        snapshot = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
    assert [(phase.count, phase.oldest_update_age_seconds) for phase in snapshot.execution] == [
        (2, 600 + index * 30) for index in range(6)]
    assert [(job.count, job.oldest_update_age_seconds) for job in snapshot.delivery] == [
        (1, 100 + index) for index in range(6)]
    registry = CollectorRegistry()
    metrics = RuntimeHealthMetrics(registry)
    metrics.publish(snapshot)
    scraped = values(registry)
    for index, phase in enumerate(phases):
        assert scraped["execution_work", (("state", phase.value),)] == 2
        assert scraped["execution_oldest_update_age_seconds", (("state", phase.value),)] == (
            600 + index * 30)
    assert scraped["delivery_jobs", (("state", "unknown"),)] == 1
    assert scraped["delivery_oldest_update_age_seconds", (("state", "unknown"),)] == 105
    exposed = generate_latest(registry).decode()
    for private in ("private-unknown-state", "private upstream message", "private/repository",
                    "private task body", "delivery-0", "implementing-old"):
        assert private not in exposed


async def test_future_execution_timestamps_and_transition_to_human_wait_recover(sessions):
    async with sessions() as session:
        row = work("future-execution", age=10000, status=WorkStatus.IMPLEMENTING)
        row.updated_at = NOW + timedelta(seconds=10)
        session.add(row)
        await session.flush()
        job = DeliveryJob(work_item_id=row.id, state="running",
                          updated_at=NOW + timedelta(seconds=10))
        session.add(job)
        await session.commit()
        before = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
        assert before.execution[2].count == before.delivery[2].count == 1
        assert before.execution[2].oldest_update_age_seconds == 0
        assert before.delivery[2].oldest_update_age_seconds == 0
        assert row.status == WorkStatus.IMPLEMENTING and job.state == "running"
        row.status, row.updated_at = WorkStatus.AWAITING_APPROVAL, NOW
        job.state, job.updated_at = "completed", NOW
        await session.commit()
        after = await read_runtime_snapshot(session, now=NOW, worker_offline_seconds=45)
    assert all(phase.count == phase.oldest_update_age_seconds == 0 for phase in after.execution)
    assert after.delivery[2].count == after.delivery[2].oldest_update_age_seconds == 0
    assert after.delivery[3].count == 1 and after.delivery[3].oldest_update_age_seconds == 0
