import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from test_migrations import HEAD_REVISION, migration_config, sqlite_url, sync_sqlite_url

from app.models import Artifact, Base, WorkItem, WorkSource, utcnow


@pytest.fixture(params=["metadata", "migration"])
def storage(tmp_path, request):
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    if request.param == "metadata":
        Base.metadata.create_all(engine)
    else:
        command.upgrade(migration_config(sqlite_url(tmp_path)), "head")
    identity = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(sa.insert(WorkItem).values(id=identity, source=WorkSource.WEB,
            title="Retention schema", requirement="Retain evidence", repository="test/retention"))
    yield engine, identity
    engine.dispose()


def values(work):
    return {"work_item_id": work, "kind": "evidence", "name": "retained.txt",
            "content_type": "text/plain", "object_key": f"{work}/artifacts/test.txt",
            "size_bytes": 3}


def test_live_pending_and_purged_states_preserve_metadata(storage):
    engine, work = storage
    now = utcnow()
    with engine.begin() as connection:
        identity = connection.scalar(sa.insert(Artifact).values(**values(work))
                                     .returning(Artifact.id))
        row = connection.execute(sa.select(Artifact)).mappings().one()
        assert all(row[key] is None for key in (
            "expired_at", "purged_at", "retention_days", "retention_sha256"))
        connection.execute(sa.update(Artifact).where(Artifact.id == identity).values(
            expired_at=now, retention_days=30, retention_sha256="a" * 64))
        connection.execute(sa.update(Artifact).where(Artifact.id == identity).values(
            purged_at=now + timedelta(seconds=1)))
        assert connection.scalar(sa.select(Artifact.name)) == "retained.txt"


@pytest.mark.parametrize("change", [
    {"expired_at": None}, {"retention_days": None}, {"retention_days": 0},
    {"retention_days": 36501}, {"retention_sha256": None}, {"retention_sha256": "short"},
    {"purged_at": "before"},
])
def test_inconsistent_retention_state_is_rejected(storage, change):
    engine, work = storage
    now = utcnow()
    data = {"expired_at": now, "retention_days": 30, "retention_sha256": "a" * 64} | change
    if data.get("purged_at") == "before":
        data["purged_at"] = now - timedelta(seconds=1)
    with pytest.raises(sa.exc.IntegrityError, match="artifact_retention_state"):
        with engine.begin() as connection:
            connection.execute(sa.insert(Artifact).values(**values(work), **data))


def test_upgrade_retains_live_rows_and_empty_state_can_downgrade(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260906_0009")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    work = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(sa.insert(WorkItem).values(id=work, source=WorkSource.WEB,
            title="Old artifact", requirement="Preserve", repository="test/retention"))
        table = sa.Table("artifacts", sa.MetaData(), autoload_with=connection)
        connection.execute(table.insert().values(id=str(uuid.uuid4()), created_at=utcnow(),
                                                 **values(work)))
        before = dict(connection.execute(sa.select(table)).mappings().one())
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(sa.select(Artifact)).mappings().one()
        assert {key: row[key] for key in before} == before
        assert row["expired_at"] is row["purged_at"] is None
    command.downgrade(config, "20260906_0009")
    with engine.connect() as connection:
        table = sa.Table("artifacts", sa.MetaData(), autoload_with=connection)
        assert dict(connection.execute(sa.select(table)).mappings().one()) == before
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()


@pytest.mark.parametrize("purged", [False, True])
def test_downgrade_cannot_erase_expiration_evidence(tmp_path, purged):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    work, now = str(uuid.uuid4()), utcnow()
    with engine.begin() as connection:
        connection.execute(sa.insert(WorkItem).values(id=work, source=WorkSource.WEB,
            title="Expired artifact", requirement="Preserve intent", repository="test/retention"))
        connection.execute(sa.insert(Artifact).values(**values(work), expired_at=now,
            purged_at=now if purged else None, retention_days=30, retention_sha256="a" * 64))
    with pytest.raises(RuntimeError, match="expiration evidence"):
        command.downgrade(config, "20260906_0009")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(Artifact.expired_at)) is not None
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == HEAD_REVISION
    command.check(config)
    engine.dispose()


def test_offline_downgrade_cannot_bypass_expiration_gate(tmp_path):
    with pytest.raises(RuntimeError, match="online state validation"):
        command.downgrade(migration_config(sqlite_url(tmp_path)),
                          "20260906_0010:20260906_0009", sql=True)
