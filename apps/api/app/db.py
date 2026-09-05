import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from .config import get_settings
from .models import Base

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
API_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_READINESS_SECONDS = 2


class SchemaState(StrEnum):
    CURRENT = "current"
    UNVERSIONED = "unversioned"
    OUTDATED = "outdated"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class SchemaReadiness:
    state: SchemaState
    current_heads: tuple[str, ...] = ()
    expected_heads: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state is SchemaState.CURRENT


def migration_scripts() -> ScriptDirectory:
    config = Config(API_ROOT / "alembic.ini")
    return ScriptDirectory.from_config(config)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def inspect_schema(target_engine: AsyncEngine) -> SchemaReadiness:
    expected_heads = tuple(sorted(migration_scripts().get_heads()))
    try:
        # Bound pool checkout/pre-ping, connection establishment, and schema lookup together.
        async with asyncio.timeout(SCHEMA_READINESS_SECONDS), target_engine.connect() as connection:
            current_heads = await connection.run_sync(
                lambda sync_connection: tuple(
                    sorted(MigrationContext.configure(sync_connection).get_current_heads())
                )
            )
    except (OSError, SQLAlchemyError):
        return SchemaReadiness(
            state=SchemaState.UNREACHABLE,
            expected_heads=expected_heads,
        )

    if not current_heads:
        state = SchemaState.UNVERSIONED
    elif current_heads != expected_heads:
        state = SchemaState.OUTDATED
    else:
        state = SchemaState.CURRENT
    return SchemaReadiness(
        state=state,
        current_heads=current_heads,
        expected_heads=expected_heads,
    )


async def get_schema_readiness() -> SchemaReadiness:
    return await inspect_schema(engine)


async def bootstrap_schema(target_engine: AsyncEngine = engine) -> None:
    scripts = migration_scripts()

    def create_and_stamp(connection: sa.Connection) -> None:
        existing_tables = set(sa.inspect(connection).get_table_names())
        application_tables = existing_tables.intersection(Base.metadata.tables)
        if application_tables:
            raise RuntimeError(
                "bootstrap mode requires an empty database; use Alembic to adopt an existing schema"
            )
        Base.metadata.create_all(connection)
        MigrationContext.configure(connection).stamp(scripts, "heads")

    async with target_engine.begin() as connection:
        await connection.run_sync(create_and_stamp)
