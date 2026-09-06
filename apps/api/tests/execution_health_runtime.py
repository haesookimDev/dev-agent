"""Owned synthetic execution rows after startup recovery; actual HTTP observation only."""

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from prometheus_client.parser import text_string_to_metric_families
from runtime_health_runtime import runtime_health_runtime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import DeliveryJob, WorkItem, WorkSource, WorkStatus, utcnow


def wait_for_startup_recovery(client):
    deadline = time.monotonic() + 15
    while True:
        response = client.get("/metrics")
        assert response.status_code == 200
        if any(sample.name == "kelpie_delivery_startup_recovery_state"
               and sample.labels.get("kelpie_delivery_startup_recovery_state") == "completed"
               and sample.value == 1
               for family in text_string_to_metric_families(response.text)
               for sample in family.samples):
            return
        assert time.monotonic() < deadline, "owned API startup recovery did not complete"
        time.sleep(0.05)


@contextmanager
def execution_health_runtime(directory, *, port=None):
    with runtime_health_runtime(directory, port=port) as runtime:
        # Never feed synthetic pending jobs to the automatic delivery recovery path.
        # This isolated API has no SCM credentials and no Worker/VM process.
        wait_for_startup_recovery(runtime.client)
        url = f"sqlite+aiosqlite:///{runtime.database}"

        async def seed():
            engine = create_async_engine(url)
            try:
                async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                    implementing = await session.scalar(select(WorkItem).where(
                        WorkItem.status == WorkStatus.IMPLEMENTING))
                    assert implementing is not None
                    implementing.updated_at = utcnow() - timedelta(minutes=40)
                    execution_ids, delivery_ids = [implementing.id], []
                    for state in ("pending", "running"):
                        past = utcnow() - timedelta(minutes=15)
                        work = WorkItem(organization_id="runtime-acceptance",
                            source=WorkSource.WEB, title=f"Synthetic {state} delivery",
                            requirement="Observe only; never publish or release a VM",
                            repository="acceptance/runtime", status=WorkStatus.COMMITTING,
                            created_at=past, updated_at=past)
                        session.add(work)
                        await session.flush()
                        session.add(DeliveryJob(work_item_id=work.id, state=state,
                            created_at=past, updated_at=past,
                            error="synthetic private delivery diagnostic"))
                        execution_ids.append(work.id)
                        delivery_ids.append(work.id)
                    await session.commit()
                    return execution_ids, delivery_ids
            finally:
                await engine.dispose()

        execution_ids, delivery_ids = asyncio.run(seed())

        def query_failure(enabled):
            # The exact table is owned by this disposable fixture, never a user database.
            statement = ("ALTER TABLE delivery_jobs RENAME TO runtime_private_delivery_fixture"
                         if enabled else
                         "ALTER TABLE runtime_private_delivery_fixture RENAME TO delivery_jobs")
            with sqlite3.connect(runtime.database) as connection:
                connection.execute(statement)

        def recover_synthetic_states():
            # This simulates observed state changes; it is not a product recovery API.
            async def recover():
                engine = create_async_engine(url)
                try:
                    async with async_sessionmaker(engine)() as session:
                        for identity in execution_ids:
                            work = await session.get(WorkItem, identity)
                            work.status, work.updated_at = WorkStatus.AWAITING_APPROVAL, utcnow()
                        for identity in delivery_ids:
                            job = await session.get(DeliveryJob, identity)
                            job.state, job.updated_at = "completed", utcnow()
                        await session.commit()
                finally:
                    await engine.dispose()
            asyncio.run(recover())

        def snapshot():
            with sqlite3.connect(runtime.database) as connection:
                return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                        for table in ("work_items", "delivery_jobs", "resource_leases",
                                      "worker_hosts", "audit_records")}

        yield SimpleNamespace(client=runtime.client, api_url=runtime.api_url,
            database=runtime.database, query_failure=query_failure,
            recover_synthetic_states=recover_synthetic_states, snapshot=snapshot)
