"""Actual pg_dump/pg_restore, current migrations, retained rows and failure atomicity."""

import asyncio
import os

import pytest
import sqlalchemy as sa
from alembic import command
from postgres_restore import fingerprint, restore_drill
from postgres_restore_seed import seed_database
from sqlalchemy.ext.asyncio import create_async_engine
from test_audit_storage import audit_values
from test_migrations import HEAD_REVISION, migration_config

from app.db import inspect_schema
from app.models import AuditRecord, Base

pytestmark = pytest.mark.skipif(
    not (os.environ.get("KELPIE_TEST_POSTGRES_URL")
         and os.environ.get("KELPIE_TEST_POSTGRES_CONTAINER")),
    reason="dedicated PostgreSQL URL and test container not set",
)


@pytest.fixture(scope="module")
def backup(tmp_path_factory):
    with restore_drill(tmp_path_factory.mktemp("postgres-restore")) as drill:
        source = drill.create_database()
        drill.migrate(source)
        seed = asyncio.run(seed_database(drill.database_url(source)))
        drill.create_reader(source)
        before = asyncio.run(fingerprint(drill.database_url(source)))
        archive = drill.backup(source)
        assert archive.stat().st_mode & 0o777 == 0o600
        assert asyncio.run(fingerprint(drill.database_url(source))) == before
        yield drill, archive, before, seed


@pytest.fixture
def restored(backup):
    drill, archive, before, seed = backup
    target = drill.create_database()
    result = drill.restore(target, archive)
    assert result.returncode == 0, "isolated pg_restore failed (output withheld)"
    return drill, target, before, seed


def test_restore_preserves_every_table_schema_owner_acl_and_migration(restored):
    drill, target, before, _ = restored
    url = drill.database_url(target)
    assert asyncio.run(fingerprint(url)) == before
    assert before[1] == set(Base.metadata.tables) | {"alembic_version"}
    command.check(migration_config(url))

    async def verify():
        engine = create_async_engine(url)
        try:
            readiness = await inspect_schema(engine)
            assert readiness.ready and readiness.current_heads == (HEAD_REVISION,)
            async with engine.connect() as connection:
                # Every table carries at least one synthetic row, not just empty-schema coverage.
                for name in Base.metadata.tables:
                    assert await connection.scalar(sa.text(f'SELECT count(*) FROM "{name}"')) > 0
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_restore_keeps_append_only_guards_identity_constraints_and_sequence(restored):
    drill, target, _, _ = restored

    async def verify():
        engine = create_async_engine(drill.database_url(target))
        try:
            for statement in (
                "UPDATE audit_records SET actor_subject = 'tampered'",
                "DELETE FROM audit_records", "TRUNCATE audit_records",
                "INSERT INTO audit_records SELECT * FROM audit_records "
                "ON CONFLICT(id) DO UPDATE SET actor_subject = 'tampered'",
            ):
                with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                    async with engine.begin() as connection:
                        await connection.exec_driver_sql(statement)
            with pytest.raises(sa.exc.IntegrityError, match="audit_actor_roles"):
                async with engine.begin() as connection:
                    await connection.execute(sa.insert(AuditRecord).values(**(
                        audit_values() | {"transport": "background"}
                    )))
            async with engine.begin() as connection:
                identity = await connection.scalar(sa.insert(AuditRecord).values(
                    **audit_values(),
                ).returning(AuditRecord.id))
                assert identity > 41
                assert await connection.scalar(sa.text("SELECT count(*) FROM audit_records")) == 3
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_restored_reader_can_read_but_cannot_change_work_or_audit(restored):
    drill, target, _, _ = restored

    async def verify():
        engine = create_async_engine(drill.database_url(target))
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(f'SET LOCAL ROLE "{drill.roles[0]}"')
                assert await connection.scalar(sa.text("SELECT count(*) FROM audit_records")) == 2
            for statement in (
                "UPDATE work_items SET status = 'COMPLETED'",
                "INSERT INTO audit_records SELECT * FROM audit_records",
                "SELECT * FROM delivery_jobs FOR UPDATE",
            ):
                with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
                    async with engine.begin() as connection:
                        await connection.exec_driver_sql(f'SET LOCAL ROLE "{drill.roles[0]}"')
                        await connection.exec_driver_sql(statement)
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_conflicting_restore_rolls_back_without_touching_existing_rows(backup):
    drill, archive, _, _ = backup
    target = drill.create_database()

    async def sentinel():
        engine = create_async_engine(drill.database_url(target))
        try:
            async with engine.begin() as connection:
                # A late-sorted existing relation conflicts after earlier restore DDL has run.
                await connection.exec_driver_sql("CREATE TABLE work_items (sentinel text)")
                await connection.exec_driver_sql("INSERT INTO work_items VALUES ('keep-me')")
        finally:
            await engine.dispose()

    asyncio.run(sentinel())
    before = asyncio.run(fingerprint(drill.database_url(target)))
    assert drill.restore(target, archive).returncode != 0
    assert asyncio.run(fingerprint(drill.database_url(target))) == before


def test_truncated_archive_does_not_leave_partial_schema(backup):
    drill, archive, _, _ = backup
    truncated = archive.with_suffix(".truncated")
    data = archive.read_bytes()
    descriptor = os.open(truncated, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    drill.archives.append(truncated)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data[:len(data) // 2])
    target = drill.create_database()
    before = asyncio.run(fingerprint(drill.database_url(target)))
    assert drill.restore(target, truncated).returncode != 0
    assert asyncio.run(fingerprint(drill.database_url(target))) == before
