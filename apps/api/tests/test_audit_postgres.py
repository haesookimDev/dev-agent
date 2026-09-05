"""Append-only checks on the real migrated PostgreSQL schema and bootstrap DDL."""

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
import sqlalchemy.ext.asyncio as async_sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_audit_storage import audit_values
from test_delivery_audit_schema import background_values, insert_legacy_job
from test_migrations import HEAD_REVISION, migration_config

from app.models import AuditRecord, Base

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture(params=["migration", "bootstrap"])
async def audit_session(request):
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if request.param == "bootstrap":
                    schema = f"audit_test_{uuid.uuid4().hex}"
                    await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                    await connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                    await connection.run_sync(Base.metadata.create_all)
                async with AsyncSession(connection, join_transaction_mode="create_savepoint") as s:
                    s.add(AuditRecord(**audit_values()))
                    await s.commit()
                    yield s
            finally:
                # Roll back only this test's rows/schema, without deleting retained audit data.
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("statement", [
    "UPDATE audit_records SET actor_subject = 'tampered'",
    "DELETE FROM audit_records",
    "TRUNCATE audit_records",
    "INSERT INTO audit_records SELECT * FROM audit_records "
    "ON CONFLICT(id) DO UPDATE SET actor_subject = 'tampered'",
])
async def test_postgres_rejects_audit_rewrites(audit_session, statement):
    before = list(await audit_session.scalars(sa.select(AuditRecord.id)))
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        await audit_session.execute(sa.text(statement))
    await audit_session.rollback()
    assert list(await audit_session.scalars(sa.select(AuditRecord.id))) == before
    assert "tampered" not in list(await audit_session.scalars(sa.select(AuditRecord.actor_subject)))
    audit_session.add(AuditRecord(**audit_values()))
    await audit_session.commit()
    assert len(list(await audit_session.scalars(sa.select(AuditRecord.id)))) == len(before) + 1


async def test_postgres_background_records_remain_append_only(audit_session):
    record = AuditRecord(**background_values())
    audit_session.add(record)
    await audit_session.flush()
    identity = record.id
    await audit_session.commit()
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        await audit_session.execute(sa.update(AuditRecord).where(
            AuditRecord.id == identity,
        ).values(details={"tampered": True}))
    await audit_session.rollback()
    retained = await audit_session.get(AuditRecord, identity, populate_existing=True)
    assert retained.transport == "background"
    assert retained.details == {"approval_audit_id": 1, "attempt": 1}


@pytest.mark.parametrize("background,change", [
    (False, {"organization_role": None}), (False, {"effective_role": None}),
    (False, {"required_role": None}), (True, {"actor_id": "forged-human"}),
    (True, {"effective_role": "administrator"}), (True, {"source_ip": "127.0.0.1"}),
])
async def test_postgres_keeps_human_and_service_identity_constraints(
    audit_session, background, change,
):
    values = background_values() if background else audit_values()
    before = list(await audit_session.scalars(sa.select(AuditRecord.id)))
    with pytest.raises(sa.exc.IntegrityError, match="audit_actor_roles"):
        await audit_session.execute(sa.insert(AuditRecord).values(**(values | change)))
    await audit_session.rollback()
    assert list(await audit_session.scalars(sa.select(AuditRecord.id))) == before


def test_postgres_upgrade_retains_historical_audit_and_legacy_delivery(monkeypatch):
    schema = f"delivery_migration_{uuid.uuid4().hex}"

    async def schema_ddl(statement):
        engine = create_async_engine(DATABASE_URL)
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(statement)
        finally:
            await engine.dispose()

    def isolated_engine(*_, **__):
        return create_async_engine(DATABASE_URL, connect_args={
            "server_settings": {"search_path": schema},
        })

    async def snapshots(seed=False):
        engine = isolated_engine()
        try:
            async with engine.begin() as connection:
                if seed:
                    await connection.run_sync(insert_legacy_job)
                    await connection.execute(sa.insert(AuditRecord).values(**audit_values()))
                audit = (await connection.execute(sa.select(AuditRecord))).mappings().one()
                job = (await connection.execute(sa.text(
                    "SELECT * FROM delivery_jobs",
                ))).mappings().one()
                revision = await connection.scalar(sa.text(
                    "SELECT version_num FROM alembic_version",
                ))
                return dict(audit), dict(job), revision
        finally:
            await engine.dispose()

    asyncio.run(schema_ddl(f'CREATE SCHEMA "{schema}"'))
    try:
        # Run real Alembic DDL with a test-owned search_path, not mocked operations.
        monkeypatch.setattr(async_sa, "async_engine_from_config", isolated_engine)
        config = migration_config(DATABASE_URL)
        command.upgrade(config, "20260906_0007")
        before_audit, before_job, _ = asyncio.run(snapshots(seed=True))
        command.upgrade(config, "head")
        command.check(config)
        after_audit, after_job, revision = asyncio.run(snapshots())
        assert before_audit == after_audit
        assert before_job == {name: after_job[name] for name in before_job}
        assert after_job["approval_audit_id"] is None
        assert revision == HEAD_REVISION
        with pytest.raises(RuntimeError, match="destroy audit records"):
            command.downgrade(config, "-1")
        assert asyncio.run(snapshots()) == (after_audit, after_job, HEAD_REVISION)
    finally:
        # Only this UUID schema is ephemeral; no shared or retained audit is deleted.
        asyncio.run(schema_ddl(f'DROP SCHEMA "{schema}" CASCADE'))
