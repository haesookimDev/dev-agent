import asyncio

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from test_migrations import HEAD_REVISION, migration_config, sqlite_url, sync_sqlite_url

from app.db import bootstrap_schema
from app.models import AuditRecord, Base, Role, utcnow


def audit_values() -> dict:
    return {
        "organization_id": "audit-test", "work_item_id": "work-snapshot",
        "repository": "audit/test", "action": "feedback.created", "target_id": "1",
        "actor_id": "principal-snapshot", "actor_subject": "test-subject",
        "identity_provider": "https://identity.example", "organization_role": Role.VIEWER,
        "repository_role": Role.OPERATOR, "effective_role": Role.OPERATOR,
        "required_role": Role.OPERATOR, "request_id": "request-snapshot",
        "correlation_id": "correlation-snapshot", "source_ip": "127.0.0.1",
        "transport": "web", "created_at": utcnow(),
    }


@pytest.fixture(params=["metadata", "migration", "bootstrap"])
def audit_engine(tmp_path, request):
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    if request.param == "metadata":
        Base.metadata.create_all(engine)
    elif request.param == "migration":
        command.upgrade(migration_config(sqlite_url(tmp_path)), "head")
    else:
        async def bootstrap():
            target = create_async_engine(sqlite_url(tmp_path))
            try:
                await bootstrap_schema(target)
            finally:
                await target.dispose()
        asyncio.run(bootstrap())
    with engine.begin() as connection:
        connection.execute(sa.insert(AuditRecord).values(id=1, **audit_values()))
    yield engine
    engine.dispose()


@pytest.mark.parametrize("statement", [
    "UPDATE audit_records SET actor_subject = 'tampered' WHERE id = 1",
    "DELETE FROM audit_records",
    "INSERT OR REPLACE INTO audit_records SELECT * FROM audit_records WHERE id = 1",
    "REPLACE INTO audit_records SELECT * FROM audit_records WHERE id = 1",
    "INSERT INTO audit_records SELECT * FROM audit_records WHERE id = 1 "
    "ON CONFLICT(id) DO UPDATE SET actor_subject = 'tampered'",
])
def test_raw_sql_cannot_rewrite_audit_records(audit_engine, statement):
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        with audit_engine.begin() as connection:
            connection.execute(sa.text(statement))
    with audit_engine.begin() as connection:
        assert connection.scalar(sa.select(AuditRecord.actor_subject)) == "test-subject"
        connection.execute(sa.insert(AuditRecord).values(**audit_values()))
        assert connection.scalar(sa.select(sa.func.count()).select_from(AuditRecord)) == 2


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_orm_cannot_rewrite_audit_records(audit_engine, mutation):
    with Session(audit_engine) as session:
        record = session.get(AuditRecord, 1)
        if mutation == "update":
            record.actor_subject = "tampered"
        else:
            session.delete(record)
        with pytest.raises(sa.exc.IntegrityError, match="append-only"):
            session.commit()
        session.rollback()
        assert session.get(AuditRecord, 1).actor_subject == "test-subject"


def test_migration_refuses_to_destroy_retained_audit_records(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.begin() as connection:
        connection.execute(sa.insert(AuditRecord).values(**audit_values()))
    with pytest.raises(RuntimeError, match="destroy audit records"):
        command.downgrade(config, "-1")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            HEAD_REVISION
        )
        assert connection.scalar(sa.select(AuditRecord.actor_subject)) == "test-subject"
    command.check(config)
    engine.dispose()


def test_audit_snapshots_do_not_depend_on_live_resources():
    assert not AuditRecord.__table__.foreign_keys


@pytest.mark.parametrize("revisions", [
    "20260906_0006:20260905_0005", "20260906_0007:20260906_0006",
])
def test_offline_downgrade_cannot_skip_the_retention_check(tmp_path, revisions):
    with pytest.raises(RuntimeError, match="online emptiness check"):
        command.downgrade(migration_config(sqlite_url(tmp_path)),
                          revisions, sql=True)


def test_details_upgrade_preserves_existing_snapshots_and_guards(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260906_0006")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    table = sa.Table("audit_records", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, **audit_values()))
        before = connection.execute(table.select()).mappings().one()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        after = connection.execute(AuditRecord.__table__.select()).mappings().one()
        assert after["details"] == {}
        assert {name: after[name] for name in before} == dict(before)
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(sa.text("UPDATE audit_records SET details = '{}' WHERE id = 1"))
    engine.dispose()


def test_empty_details_downgrade_restores_previous_guards(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    command.downgrade(config, "20260906_0006")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    table = sa.Table("audit_records", sa.MetaData(), autoload_with=engine)
    assert "details" not in table.columns
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, **audit_values()))
    for statement in ("UPDATE audit_records SET actor_subject = 'tampered'",
                      "DELETE FROM audit_records",
                      "INSERT OR REPLACE INTO audit_records SELECT * FROM audit_records"):
        with pytest.raises(sa.exc.IntegrityError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(sa.text(statement))
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
