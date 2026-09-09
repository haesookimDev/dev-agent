import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from test_migrations import migration_config, sqlite_url, sync_sqlite_url

from app.models import ArtifactRetentionJob, Base, WebhookDelivery, utcnow


@pytest.fixture(params=["metadata", "migration"])
def storage(tmp_path, request):
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    if request.param == "metadata":
        Base.metadata.create_all(engine)
    else:
        command.upgrade(migration_config(sqlite_url(tmp_path)), "head")
    try:
        yield engine
    finally:
        engine.dispose()


def test_checkpoint_keeps_cursor_and_version_across_connections(storage):
    cursor, completed = str(uuid.uuid4()), utcnow()
    with storage.begin() as connection:
        connection.execute(sa.insert(ArtifactRetentionJob).values(scope_key="a" * 64))
        row = connection.execute(sa.select(ArtifactRetentionJob)).mappings().one()
        assert row["version"] == 1 and row["cursor"] is row["sweep_completed_at"] is None
        connection.execute(sa.update(ArtifactRetentionJob).values(
            cursor=cursor, version=2, updated_at=completed, sweep_completed_at=completed))
    with storage.connect() as connection:
        row = connection.execute(sa.select(ArtifactRetentionJob)).mappings().one()
        assert row["cursor"] == cursor and row["version"] == 2
        assert row["sweep_completed_at"] is not None


@pytest.mark.parametrize("change", [
    {"scope_key": "short"}, {"version": 0}, {"version": -1}, {"cursor": "invalid"},
])
def test_invalid_progress_is_rejected(storage, change):
    with pytest.raises(sa.exc.IntegrityError, match="retention_job_"):
        with storage.begin() as connection:
            connection.execute(sa.insert(ArtifactRetentionJob).values(
                **({"scope_key": "a" * 64} | change)))
    with storage.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(ArtifactRetentionJob)) == 0


def test_progress_migration_and_rollback_leave_existing_data_unchanged(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260906_0010")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    try:
        with engine.begin() as connection:
            connection.execute(sa.insert(WebhookDelivery).values(
                delivery_id="retained-delivery", event_name="issues"))
            before = connection.execute(sa.select(WebhookDelivery)).all()
        command.upgrade(config, "head")
        command.check(config)
        with engine.begin() as connection:
            connection.execute(sa.insert(ArtifactRetentionJob).values(scope_key="a" * 64))
        command.downgrade(config, "20260906_0010")
        assert "artifact_retention_jobs" not in sa.inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(sa.select(WebhookDelivery)).all() == before
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
