import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine
from test_migrations import migration_config, sqlite_url, sync_sqlite_url

from app.db import SchemaState, bootstrap_schema, inspect_schema
from app.models import Base, WebhookDelivery


async def schema_state(database_url: str) -> SchemaState:
    target_engine = create_async_engine(database_url)
    try:
        return (await inspect_schema(target_engine)).state
    finally:
        await target_engine.dispose()


def test_schema_is_ready_only_at_migration_head(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path)
    assert asyncio.run(schema_state(database_url)) is SchemaState.UNVERSIONED

    command.upgrade(migration_config(database_url), "head")

    assert asyncio.run(schema_state(database_url)) is SchemaState.CURRENT


def test_schema_with_unknown_revision_is_outdated(tmp_path: Path) -> None:
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES ('unknown-revision')")
        )
    engine.dispose()

    assert asyncio.run(schema_state(sqlite_url(tmp_path))) is SchemaState.OUTDATED


def test_explicit_bootstrap_creates_and_stamps_empty_database(tmp_path: Path) -> None:
    async def bootstrap() -> SchemaState:
        target_engine = create_async_engine(sqlite_url(tmp_path))
        try:
            await bootstrap_schema(target_engine)
            return (await inspect_schema(target_engine)).state
        finally:
            await target_engine.dispose()

    assert asyncio.run(bootstrap()) is SchemaState.CURRENT
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    assert set(Base.metadata.tables).issubset(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_explicit_bootstrap_rejects_nonempty_database(tmp_path: Path) -> None:
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    WebhookDelivery.__table__.create(engine)
    engine.dispose()

    async def bootstrap() -> None:
        target_engine = create_async_engine(sqlite_url(tmp_path))
        try:
            await bootstrap_schema(target_engine)
        finally:
            await target_engine.dispose()

    with pytest.raises(RuntimeError, match="requires an empty database"):
        asyncio.run(bootstrap())
