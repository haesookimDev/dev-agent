from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.models import Base, WebhookDelivery

API_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "20260904_0001"


def migration_config(database_url: str) -> Config:
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}"


def sync_sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migration.db'}"


def current_revision(engine: sa.Engine) -> str | None:
    with engine.connect() as connection:
        result = connection.execute(sa.text("SELECT version_num FROM alembic_version"))
        return result.scalar_one_or_none()


def test_empty_database_upgrades_to_head(tmp_path: Path) -> None:
    config = migration_config(sqlite_url(tmp_path))

    command.upgrade(config, "head")

    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    expected_tables = set(Base.metadata.tables) | {"alembic_version"}
    assert set(sa.inspect(engine).get_table_names()) == expected_tables
    assert current_revision(engine) == HEAD_REVISION
    command.check(config)
    engine.dispose()


def test_existing_create_all_schema_is_adopted_without_data_loss(tmp_path: Path) -> None:
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(WebhookDelivery).values(
                delivery_id="existing-delivery",
                event_name="issues",
            )
        )

    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")

    with engine.connect() as connection:
        deliveries = connection.execute(sa.select(WebhookDelivery.delivery_id)).scalars().all()
    assert deliveries == ["existing-delivery"]
    assert current_revision(engine) == HEAD_REVISION
    command.check(config)
    engine.dispose()


def test_partial_legacy_schema_is_not_stamped_and_upgrade_can_be_retried(tmp_path: Path) -> None:
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    WebhookDelivery.__table__.create(engine)
    config = migration_config(sqlite_url(tmp_path))

    with pytest.raises(RuntimeError, match="partial Kelpie schema"):
        command.upgrade(config, "head")

    WebhookDelivery.__table__.drop(engine)
    command.upgrade(config, "head")
    assert current_revision(engine) == HEAD_REVISION
    engine.dispose()


def test_baseline_downgrade_refuses_destructive_rollback(tmp_path: Path) -> None:
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))

    with pytest.raises(RuntimeError, match="destroy all Kelpie data"):
        command.downgrade(config, "base")

    assert current_revision(engine) == HEAD_REVISION
    assert "work_items" in sa.inspect(engine).get_table_names()
    engine.dispose()
