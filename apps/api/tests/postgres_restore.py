"""PostgreSQL dump/restore drill limited to databases it successfully creates itself."""

import asyncio
import hashlib
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine
from test_migrations import migration_config


class RestoreDrill:
    def __init__(self, directory: Path):
        self.directory = directory
        self.url = sa.make_url(os.environ["KELPIE_TEST_POSTGRES_URL"])
        self.container = os.environ["KELPIE_TEST_POSTGRES_CONTAINER"]
        self.databases = []
        self.roles = []
        self.archives = []

    def tool(self, executable, database, *arguments, stdin=None, stdout=None):
        # No DSN/password in argv; the explicitly selected test container supplies local auth.
        result = subprocess.run(
            ["docker", "exec", "-i", self.container, executable, "--no-password",
             "--username", self.url.username, "--dbname", database, *arguments],
            stdin=stdin, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        return result

    def database_url(self, database):
        assert database in self.databases, "database is not owned by this drill"
        return self.url.set(database=database).render_as_string(hide_password=False)

    def create_database(self):
        database = f"kelpie_restore_{uuid.uuid4().hex}"

        async def create():
            engine = create_async_engine(self.url, isolation_level="AUTOCOMMIT")
            try:
                async with engine.connect() as connection:
                    await connection.exec_driver_sql(
                        f'CREATE DATABASE "{database}" TEMPLATE template0',
                    )
            finally:
                await engine.dispose()

        asyncio.run(create())
        # Record ownership only after successful creation; collisions never authorize cleanup.
        self.databases.append(database)
        return database

    def migrate(self, database):
        command.upgrade(migration_config(self.database_url(database)), "head")

    def create_reader(self, database):
        role = f"kelpie_reader_{uuid.uuid4().hex}"

        async def grant():
            engine = create_async_engine(self.database_url(database))
            try:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER '
                        'NOCREATEDB NOCREATEROLE NOINHERIT',
                    )
                    await connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
                    await connection.exec_driver_sql(
                        f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"',
                    )
                self.roles.append(role)
            finally:
                await engine.dispose()

        asyncio.run(grant())
        return role

    def backup(self, database):
        assert database in self.databases
        archive = self.directory / f"{database}.dump"
        descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.archives.append(archive)
        with os.fdopen(descriptor, "wb") as output:
            result = self.tool("pg_dump", database, "--format=custom", stdout=output)
        assert result.returncode == 0, "isolated pg_dump failed (output withheld)"
        return archive

    def restore(self, database, archive):
        assert database in self.databases
        with archive.open("rb") as source:
            return self.tool("pg_restore", database, "--single-transaction", "--exit-on-error",
                             stdin=source)

    def cleanup(self):
        async def drop():
            engine = create_async_engine(self.url, isolation_level="AUTOCOMMIT")
            try:
                async with engine.connect() as connection:
                    for database in reversed(self.databases):
                        # No FORCE or connection termination: a live consumer is a cleanup error.
                        await connection.exec_driver_sql(f'DROP DATABASE "{database}"')
                    for role in reversed(self.roles):
                        await connection.exec_driver_sql(f'DROP ROLE "{role}"')
            finally:
                await engine.dispose()

        asyncio.run(drop())


@contextmanager
def restore_drill(directory):
    drill = RestoreDrill(directory)
    try:
        yield drill
    finally:
        try:
            drill.cleanup()
        finally:
            for archive in drill.archives:
                archive.unlink()


async def fingerprint(database_url):
    """Compare every application row plus constraints/indexes/triggers/ownership/ACLs privately."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
            digest = hashlib.sha256()
            for table in sorted(tables):
                quoted = connection.dialect.identifier_preparer.quote(table)
                rows = await connection.scalars(sa.text(
                    f"SELECT to_jsonb(t)::text FROM {quoted} t",
                ))
                for row in [table, *sorted(rows)]:
                    digest.update(row.encode() + b"\n")
            catalog = await connection.scalars(sa.text("""
                SELECT jsonb_build_array('relation', c.relname, c.relkind,
                    pg_get_userbyid(c.relowner), coalesce(c.relacl,
                        acldefault(CASE WHEN c.relkind = 'S' THEN 's'::"char"
                            ELSE 'r'::"char" END, c.relowner)))::text
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                UNION ALL
                SELECT jsonb_build_array('constraint', c.conname,
                    c.conrelid::regclass::text, pg_get_constraintdef(c.oid))::text
                FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE n.nspname = 'public'
                UNION ALL
                SELECT jsonb_build_array('trigger', t.tgname, pg_get_triggerdef(t.oid))::text
                FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND NOT t.tgisinternal
                UNION ALL
                SELECT jsonb_build_array('index', indexname, indexdef)::text
                FROM pg_indexes WHERE schemaname = 'public'
                UNION ALL
                SELECT jsonb_build_array('column', c.relname, a.attname,
                    format_type(a.atttypid, a.atttypmod), a.attnotnull,
                    pg_get_expr(d.adbin, d.adrelid))::text
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                    AND a.attnum > 0 AND NOT a.attisdropped
                UNION ALL
                SELECT jsonb_build_array('sequence', sequencename, sequenceowner,
                    data_type, start_value, min_value, max_value, increment_by,
                    cycle, cache_size, last_value)::text
                FROM pg_sequences WHERE schemaname = 'public'
                UNION ALL
                SELECT jsonb_build_array('function', p.proname, pg_get_functiondef(p.oid))::text
                FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
            """))
            for row in sorted(normalize_constraint_casts(row) for row in catalog):
                digest.update(row.encode() + b"\n")
            return digest.hexdigest(), set(tables)
    finally:
        await engine.dispose()


def normalize_constraint_casts(row):
    # PostgreSQL reparses varchar-literal array casts on restore: casting the literal
    # array or each element to text is equivalent. Preserve every other expression.
    if row.startswith('["constraint",'):
        literal = r"'(?:[^']|'')*'::character varying"
        row = re.sub(rf"\(\(ARRAY\[({literal}(?:, {literal})*)\]\)::text\[\]\)",
                     r"(ARRAY[\1])", row)
        row = re.sub(rf"\(({literal})\)::text", r"\1", row)
    return row
