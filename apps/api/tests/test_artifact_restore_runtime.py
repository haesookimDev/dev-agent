import os

import pytest
from artifact_restore_runtime import artifact_restore_runtime

from app.artifact_backup import COMPLETE, digest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("KELPIE_TEST_POSTGRES_URL")
         and os.environ.get("KELPIE_TEST_POSTGRES_CONTAINER")),
    reason="dedicated PostgreSQL URL and test container not set",
)


def test_real_database_and_artifact_restore_preserves_content_and_access(tmp_path):
    with artifact_restore_runtime(tmp_path) as runtime:
        client = runtime.client
        urls = {label: f"/api/work-items/{work}/artifacts/{identity}"
                for label, (identity, work, _, _) in runtime.evidence.items()}
        for label in ("image", "text"):
            assert client.get(urls[label]).status_code == 410
        assert client.get(urls["foreign"]).status_code == 404
        assert not runtime.root.exists()
        result = runtime.restore_files()
        assert result == {"artifacts": 3, "files": 3, "restored": True, "verified": True}
        assert (runtime.root / COMPLETE).is_file()
        for label in ("image", "text"):
            response = client.get(urls[label])
            assert response.status_code == 200
            assert response.content == runtime.evidence[label][3]
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["content-security-policy"] == "sandbox"
        assert client.get(urls["foreign"]).status_code == 404
        assert client.get(f"/api/work-items/{runtime.seed.work_id}/audit-log").status_code == 403
        client.cookies.clear()
        assert client.get(urls["text"]).status_code == 401
        assert runtime.cli("verify-restored")["verified"]


def test_real_restore_rejects_tamper_and_retains_existing_root(tmp_path):
    with artifact_restore_runtime(tmp_path) as runtime:
        runtime.stop()
        content = runtime.evidence["text"][3]
        blob = runtime.snapshot / "blobs" / digest(content)
        blob.write_bytes(b"x" * len(content))
        args = ("--backup", str(runtime.snapshot), "--output", str(runtime.root),
                "--writers-stopped")
        runtime.cli("restore", *args, expected=2)
        assert not runtime.root.exists()
        blob.write_bytes(content)
        runtime.root.mkdir()
        sentinel = runtime.root / "keep"
        sentinel.write_bytes(b"keep existing candidate")
        runtime.cli("restore", *args, expected=2)
        assert sentinel.read_bytes() == b"keep existing candidate"
        assert not (runtime.root / COMPLETE).exists()


def test_real_postgres_and_file_backup_keep_retention_evidence_without_resurrection(tmp_path):
    import asyncio
    import json
    import subprocess
    import sys

    import sqlalchemy as sa
    from artifact_restore_runtime import ROOT
    from artifact_retention_case import seed
    from postgres_restore import fingerprint, restore_drill
    from postgres_restore_runtime import reader_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.artifact_backup_admin import read_records
    from app.artifact_storage import write_artifact_content
    from app.models import utcnow

    with restore_drill(tmp_path) as drill:
        source = drill.create_database()
        drill.migrate(source)
        source_url = drill.database_url(source)
        source_root = tmp_path / "source-objects"
        live_content = b"Recent evidence remains available\n"

        async def setup():
            engine = create_async_engine(source_url)
            try:
                case = await seed(async_sessionmaker(engine, expire_on_commit=False), source_root)
                key = case.key + ".new"
                await case.alias(object_key=key, size_bytes=len(live_content), created_at=utcnow())
                write_artifact_content(str(source_root), case.work, key, live_content)
                return case.work, case.key, key, case.digest
            finally:
                await engine.dispose()

        work, expired_key, live_key, expired_digest = asyncio.run(setup())
        environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
                       "DATABASE_URL": source_url, "ARTIFACT_ROOT": str(source_root)}

        def cli(module, *args, overrides=None):
            selected = environment | (overrides or {})
            result = subprocess.run([sys.executable, "-m", module, *args],
                env=selected, cwd=tmp_path, capture_output=True, timeout=20)
            assert result.returncode == 0, "owned retention recovery command failed (withheld)"
            for private in (selected["DATABASE_URL"].encode(), str(tmp_path).encode(),
                            live_content):
                assert private not in result.stdout + result.stderr
            return json.loads(result.stdout)

        args = ("--retain-days", "30", "--work-id", work)
        assert cli("app.artifact_retention_admin", *args)["counts"] == {
            "eligible": 1, "protected": 1}
        result = cli("app.artifact_retention_admin", *args, "--apply")
        assert result["counts"]["purged"] == 1 and result["counts"]["protected"] == 1
        assert not (source_root / expired_key).exists()
        assert (source_root / live_key).read_bytes() == live_content
        role = drill.create_reader(source)
        source_reader = reader_url(drill, source, role)
        before = asyncio.run(fingerprint(source_url))
        dump = drill.backup(source)
        snapshot = tmp_path / "snapshot"
        backed_up = cli("app.artifact_backup_admin", "create", "--database-dump", str(dump),
            "--output", str(snapshot), "--writers-stopped",
            overrides={"DATABASE_URL": source_reader})
        assert backed_up["artifacts"] == 2 and backed_up["blobs"] == 1
        assert not (snapshot / "blobs" / expired_digest).exists()
        target = drill.create_database()
        assert drill.restore(target, dump).returncode == 0, "retention database restore failed"
        target_url = drill.database_url(target)
        assert asyncio.run(fingerprint(target_url)) == before
        target_reader = sa.make_url(source_reader).set(database=target).render_as_string(
            hide_password=False)
        restored_root = tmp_path / "restored-objects"
        common = ("--database-dump", str(dump), "--manifest-sha256", backed_up["manifest_sha256"])
        override = {"DATABASE_URL": target_reader, "ARTIFACT_ROOT": str(restored_root)}
        restored = cli("app.artifact_backup_admin", "restore", *common, "--backup", str(snapshot),
            "--output", str(restored_root), "--writers-stopped", overrides=override)
        assert restored == {"artifacts": 2, "files": 1, "restored": True, "verified": True}
        assert cli("app.artifact_backup_admin", "verify-restored", *common,
                   overrides=override)["verified"]
        assert not (restored_root / expired_key).exists()
        assert (restored_root / live_key).read_bytes() == live_content

        async def inventory():
            engine = create_async_engine(target_reader)
            try:
                return await read_records(async_sessionmaker(engine))
            finally:
                await engine.dispose()
        expired = next(row for row in asyncio.run(inventory()) if row.object_key == expired_key)
        assert expired.expired_at and expired.purged_at and expired.retention_days == 30
        assert expired.retention_sha256 == expired_digest
        assert asyncio.run(fingerprint(source_url)) == before
        assert asyncio.run(fingerprint(target_url)) == before
