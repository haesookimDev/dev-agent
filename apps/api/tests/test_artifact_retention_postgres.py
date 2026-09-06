"""Retention constraints and rollback gates on real, isolated PostgreSQL DDL."""

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
import sqlalchemy.ext.asyncio as async_sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_artifact_retention_schema import values
from test_migrations import HEAD_REVISION, migration_config

from app.models import Artifact, Base, Organization, WorkItem, WorkSource, utcnow

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture(params=["migration", "metadata"])
async def retention_session(request):
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if request.param == "metadata":
                    schema = f"retention_test_{uuid.uuid4().hex}"
                    await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                    await connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                    await connection.run_sync(Base.metadata.create_all)
                    await connection.execute(sa.insert(Organization).values(id="legacy"))
                async with AsyncSession(connection, join_transaction_mode="create_savepoint",
                                        expire_on_commit=False) as session:
                    work = WorkItem(source=WorkSource.WEB, title="Retention constraint",
                                    requirement="Preserve metadata", repository="test/retention")
                    session.add(work)
                    await session.flush()
                    artifact = Artifact(**values(work.id))
                    session.add(artifact)
                    await session.commit()
                    yield session, artifact.id
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", [
    {"expired_at": None}, {"retention_days": None}, {"retention_days": 0},
    {"retention_days": 36501}, {"retention_sha256": None}, {"retention_sha256": "short"},
])
async def test_postgres_rejects_incomplete_expiration_evidence(retention_session, change):
    session, identity = retention_session
    with pytest.raises(sa.exc.IntegrityError, match="artifact_retention_state"):
        await session.execute(sa.update(Artifact).where(Artifact.id == identity).values(
            **({"expired_at": utcnow(), "retention_days": 30,
                "retention_sha256": "a" * 64} | change)))
    await session.rollback()
    row = await session.get(Artifact, identity, populate_existing=True)
    assert row.expired_at is row.purged_at is None


async def test_postgres_retains_both_deletion_phases(retention_session):
    session, identity = retention_session
    now = utcnow()
    await session.execute(sa.update(Artifact).where(Artifact.id == identity).values(
        expired_at=now, retention_days=30, retention_sha256="a" * 64))
    await session.commit()
    row = await session.get(Artifact, identity, populate_existing=True)
    assert row.expired_at == now and row.purged_at is None
    await session.execute(sa.update(Artifact).where(Artifact.id == identity).values(purged_at=now))
    await session.commit()
    row = await session.get(Artifact, identity, populate_existing=True)
    assert row.purged_at == now and row.name == "retained.txt"


def test_postgres_migration_keeps_live_rows_and_blocks_expired_downgrade(monkeypatch):
    schema = f"retention_migration_{uuid.uuid4().hex}"

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

    async def snapshots(seed=False, expire=False):
        engine = isolated_engine()
        try:
            async with engine.begin() as connection:
                if seed:
                    work = str(uuid.uuid4())
                    await connection.execute(sa.insert(WorkItem).values(id=work,
                        source=WorkSource.WEB, title="Legacy artifact", requirement="Preserve",
                        repository="test/retention"))
                    table = await connection.run_sync(lambda sync: sa.Table(
                        "artifacts", sa.MetaData(), autoload_with=sync))
                    await connection.execute(table.insert().values(id=str(uuid.uuid4()),
                        created_at=utcnow(), **values(work)))
                if expire:
                    await connection.execute(sa.update(Artifact).values(
                        expired_at=utcnow(), retention_days=30, retention_sha256="a" * 64))
                result = await connection.execute(sa.text("SELECT * FROM artifacts"))
                row = result.mappings().one()
                revision = await connection.scalar(sa.text(
                    "SELECT version_num FROM alembic_version"))
                return dict(row), revision
        finally:
            await engine.dispose()

    asyncio.run(schema_ddl(f'CREATE SCHEMA "{schema}"'))
    try:
        monkeypatch.setattr(async_sa, "async_engine_from_config", isolated_engine)
        config = migration_config(DATABASE_URL)
        command.upgrade(config, "20260906_0009")
        before, _ = asyncio.run(snapshots(seed=True))
        command.upgrade(config, "head")
        command.check(config)
        after, revision = asyncio.run(snapshots())
        assert revision == HEAD_REVISION
        assert {key: after[key] for key in before} == before
        command.downgrade(config, "20260906_0009")
        assert asyncio.run(snapshots())[0] == before
        command.upgrade(config, "head")
        expired = asyncio.run(snapshots(expire=True))
        with pytest.raises(RuntimeError, match="expiration evidence"):
            command.downgrade(config, "20260906_0009")
        assert asyncio.run(snapshots()) == expired
        command.check(config)
    finally:
        asyncio.run(schema_ddl(f'DROP SCHEMA "{schema}" CASCADE'))
