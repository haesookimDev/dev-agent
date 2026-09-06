import pytest
import sqlalchemy as sa
from alembic import command
from artifact_runtime import artifact_runtime
from migration_runtime import refuse_downgrade
from test_migrations import migration_config, sqlite_url, sync_sqlite_url


def test_failed_multi_revision_sqlite_downgrade_keeps_entire_schema(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260904_0003")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.connect() as connection:
        before = connection.execute(sa.text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )).all()
    with pytest.raises(RuntimeError, match="destroy all Kelpie data"):
        command.downgrade(config, "base")
    with engine.connect() as connection:
        after = connection.execute(sa.text(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )).all()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == "20260904_0003"
        assert after == before
    engine.dispose()


def test_failed_real_migration_preserves_live_http_access_and_isolation(tmp_path):
    with artifact_runtime(tmp_path) as runtime:
        assert refuse_downgrade(runtime)["schema_and_rows"] == "unchanged"
