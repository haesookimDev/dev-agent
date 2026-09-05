"""Append-only checks on the real migrated PostgreSQL schema and bootstrap DDL."""

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_audit_storage import audit_values

from app.models import AuditRecord, Base

DATABASE_URL = os.environ.get("KELPIE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated PostgreSQL test URL not set")


@pytest.fixture(params=["migration", "bootstrap"])
async def audit_session(request):
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                if request.param == "bootstrap":
                    schema = f"audit_test_{uuid.uuid4().hex}"
                    await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
                    await connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                    await connection.run_sync(Base.metadata.create_all)
                async with AsyncSession(connection, join_transaction_mode="create_savepoint") as s:
                    s.add(AuditRecord(**audit_values()))
                    await s.commit()
                    yield s
            finally:
                # Roll back only this test's rows/schema, without deleting retained audit data.
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("statement", [
    "UPDATE audit_records SET actor_subject = 'tampered'",
    "DELETE FROM audit_records",
    "TRUNCATE audit_records",
    "INSERT INTO audit_records SELECT * FROM audit_records "
    "ON CONFLICT(id) DO UPDATE SET actor_subject = 'tampered'",
])
async def test_postgres_rejects_audit_rewrites(audit_session, statement):
    before = list(await audit_session.scalars(sa.select(AuditRecord.id)))
    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
        await audit_session.execute(sa.text(statement))
    await audit_session.rollback()
    assert list(await audit_session.scalars(sa.select(AuditRecord.id))) == before
    assert "tampered" not in list(await audit_session.scalars(sa.select(AuditRecord.actor_subject)))
    audit_session.add(AuditRecord(**audit_values()))
    await audit_session.commit()
    assert len(list(await audit_session.scalars(sa.select(AuditRecord.id)))) == len(before) + 1
