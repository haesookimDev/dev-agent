import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session
from test_audit_storage import audit_engine as audit_engine
from test_audit_storage import audit_values
from test_migrations import HEAD_REVISION, migration_config, sqlite_url, sync_sqlite_url

from app.models import AuditRecord, DeliveryJob, Role, WorkItem, WorkSource, WorkStatus, utcnow
from app.schemas import AuditRecordView


def background_values():
    return audit_values() | {
        "actor_id": None, "actor_subject": "delivery:github",
        "identity_provider": "urn:kelpie:service", "organization_role": None,
        "repository_role": None, "effective_role": None, "required_role": None,
        "transport": "background", "source_ip": None, "action": "delivery.started",
        "details": {"approval_audit_id": 1, "attempt": 1},
    }


def test_background_audit_has_explicit_service_identity_and_retains_immutability(audit_engine):
    with Session(audit_engine) as session:
        record = AuditRecord(**background_values())
        session.add(record)
        session.commit()
        identity = record.id
        view = AuditRecordView.model_validate(record)
        assert view.transport == "background"
        assert view.actor_subject == "delivery:github"
        assert view.actor_id is view.source_ip is view.required_role is None
        assert view.details == {"approval_audit_id": 1, "attempt": 1}
    for sql in (
        "UPDATE audit_records SET details = '{}' WHERE id = :id",
        "DELETE FROM audit_records WHERE id = :id",
    ):
        with pytest.raises(sa.exc.IntegrityError, match="append-only"):
            with audit_engine.begin() as connection:
                connection.execute(sa.text(sql), {"id": identity})


@pytest.mark.parametrize("transport", ["web", "slack"])
@pytest.mark.parametrize("field", ["organization_role", "effective_role", "required_role"])
def test_human_audits_still_require_role_snapshots(audit_engine, transport, field):
    with pytest.raises(sa.exc.IntegrityError, match="audit_actor_roles"):
        with audit_engine.begin() as connection:
            connection.execute(sa.insert(AuditRecord).values(
                **(audit_values() | {"transport": transport, field: None}),
            ))


@pytest.mark.parametrize("field,value", [
    ("actor_id", "human-principal"), ("source_ip", "127.0.0.1"),
    ("organization_role", Role.ADMINISTRATOR), ("repository_role", Role.APPROVER),
    ("effective_role", Role.APPROVER), ("required_role", Role.APPROVER),
])
def test_background_audits_cannot_impersonate_human_roles(audit_engine, field, value):
    with pytest.raises(sa.exc.IntegrityError, match="audit_actor_roles"):
        with audit_engine.begin() as connection:
            connection.execute(sa.insert(AuditRecord).values(
                **(background_values() | {field: value}),
            ))


def insert_legacy_job(connection):
    identity = "11111111-1111-4111-8111-111111111111"
    connection.execute(sa.insert(WorkItem).values(
        id=identity, source=WorkSource.WEB, title="Existing delivery", requirement="Retain",
        repository="acme/existing", status=WorkStatus.COMMITTING,
    ))
    table = sa.Table("delivery_jobs", sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(
        work_item_id=identity, state="running", attempts=2, created_at=utcnow(),
        updated_at=utcnow(), error=None,
    ))
    return identity


def test_upgrade_preserves_old_audits_and_does_not_invent_delivery_authority(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "20260906_0007")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.begin() as connection:
        identity = insert_legacy_job(connection)
        connection.execute(sa.insert(AuditRecord).values(**audit_values()))
        before_audit = connection.execute(sa.select(AuditRecord)).mappings().one()
        before_job = connection.execute(sa.text("SELECT * FROM delivery_jobs")).mappings().one()
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        after_audit = connection.execute(sa.select(AuditRecord)).mappings().one()
        assert dict(after_audit) == dict(before_audit)
        job = connection.execute(sa.text("SELECT * FROM delivery_jobs")).mappings().one()
        assert {name: job[name] for name in before_job} == dict(before_job)
        assert job["approval_audit_id"] is None and job["work_item_id"] == identity
    with pytest.raises(RuntimeError, match="destroy audit records"):
        command.downgrade(config, "20260906_0007")
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == HEAD_REVISION
    engine.dispose()


def test_empty_downgrade_restores_human_only_schema_and_guards(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    command.downgrade(config, "20260906_0007")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    inspector = sa.inspect(engine)
    assert "approval_audit_id" not in {c["name"] for c in inspector.get_columns("delivery_jobs")}
    roles = {c["name"]: c["nullable"] for c in inspector.get_columns("audit_records")}
    assert not any(roles[name] for name in ("organization_role", "effective_role", "required_role"))
    with engine.begin() as connection:
        connection.execute(sa.insert(AuditRecord).values(**audit_values()))
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM audit_records"))
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()


def test_downgrade_retains_nonempty_delivery_provenance_even_without_source_audit(tmp_path):
    config = migration_config(sqlite_url(tmp_path))
    command.upgrade(config, "head")
    engine = sa.create_engine(sync_sqlite_url(tmp_path))
    with engine.begin() as connection:
        identity = insert_legacy_job(connection)
        connection.execute(sa.update(DeliveryJob).values(approval_audit_id=42))
    with pytest.raises(RuntimeError, match="delivery approval provenance"):
        command.downgrade(config, "20260906_0007")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(DeliveryJob.approval_audit_id).where(
            DeliveryJob.work_item_id == identity,
        )) == 42
    engine.dispose()
