from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.models import Base, WebhookDelivery

API_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "20260905_0004"


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
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260904_0001")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM alembic_version"))
        connection.execute(
            sa.insert(WebhookDelivery).values(
                delivery_id="existing-delivery",
                event_name="issues",
            )
        )
    command.upgrade(config, "head")

    with engine.connect() as connection:
        deliveries = connection.execute(sa.select(WebhookDelivery.delivery_id)).scalars().all()
    assert deliveries == ["existing-delivery"]
    assert current_revision(engine) == HEAD_REVISION
    command.check(config)
    engine.dispose()


def test_correlation_migration_backfills_existing_work_and_events(tmp_path: Path) -> None:
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260904_0001")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    metadata = sa.MetaData()
    metadata.reflect(engine)
    work_id = "11111111-1111-4111-8111-111111111111"
    created_at = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["work_items"].insert().values(
                id=work_id,
                source="WEB",
                title="Existing work",
                requirement="Keep existing data",
                repository="acme/service",
                status="QUEUED",
                version=1,
                requested_by="test",
                budget_minutes=240,
                replan_limit=3,
                approval_required=True,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        connection.execute(
            metadata.tables["agent_events"].insert().values(
                work_item_id=work_id,
                event_type="work.created",
                source="test",
                level="info",
                message="created",
                payload={},
                created_at=created_at,
            )
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        work_correlation = connection.execute(
            sa.text("SELECT correlation_id FROM work_items WHERE id = :id"), {"id": work_id}
        ).scalar_one()
        event_correlation = connection.execute(
            sa.text("SELECT correlation_id FROM agent_events WHERE work_item_id = :id"),
            {"id": work_id},
        ).scalar_one()
    assert work_correlation == work_id
    assert event_correlation == work_id
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT organization_id FROM work_items WHERE id = :id"
        ), {"id": work_id}).scalar_one() == "legacy"
        assert connection.execute(sa.text("SELECT count(*) FROM memberships")).scalar_one() == 0
    command.downgrade(config, "20260904_0003")
    with engine.connect() as connection:
        assert connection.execute(sa.text(
            "SELECT title FROM work_items WHERE id = :id"
        ), {"id": work_id}).scalar_one() == "Existing work"
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


def test_correlation_downgrade_is_safe_and_baseline_downgrade_is_refused(
    tmp_path: Path,
) -> None:
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))

    command.downgrade(config, "20260904_0001")
    assert current_revision(engine) == "20260904_0001"
    assert "correlation_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns("work_items")
    }

    with pytest.raises(RuntimeError, match="destroy all Kelpie data"):
        command.downgrade(config, "base")

    assert current_revision(engine) == "20260904_0001"
    assert "work_items" in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_oidc_session_migration_downgrade_preserves_existing_work(tmp_path: Path) -> None:
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))

    command.downgrade(config, "20260904_0002")

    tables = set(sa.inspect(engine).get_table_names())
    assert "auth_sessions" not in tables
    assert "oidc_login_attempts" not in tables
    assert "work_items" in tables
    assert current_revision(engine) == "20260904_0002"
    engine.dispose()
