"""Owned PostgreSQL + file restore drill. No production writes, external IdP, SCM or VM."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import sqlalchemy as sa
from artifact_content_runtime import png_evidence
from postgres_restore import fingerprint, restore_drill
from postgres_restore_runtime import reader_url
from postgres_restore_seed import ISSUER, RestoreSeed, seed_database
from sqlalchemy.ext.asyncio import create_async_engine
from stream_runtime_checks import assert_stream_log_clean

from app.artifact_backup import write_new
from app.models import Artifact

ROOT = Path(__file__).resolve().parents[3]
TEXT = "합성 산출물 복원 완료 / Synthetic artifact restored\n".encode()


async def seed_files(url, root, seed):
    engine = create_async_engine(url)
    evidence = {}
    try:
        async with engine.begin() as connection:
            image = (await connection.execute(sa.select(Artifact.id, Artifact.object_key)
                     .where(Artifact.work_item_id == seed.work_id))).one()
            png = png_evidence()
            await connection.execute(sa.update(Artifact).where(Artifact.id == image.id)
                .values(size_bytes=len(png)))
            evidence["image"] = (image.id, seed.work_id, image.object_key, png)
            for label, work, content in (("text", seed.work_id, TEXT),
                                         ("foreign", seed.other_id, b"Foreign synthetic file\n")):
                identity = str(uuid.uuid4())
                key = f"{work}/artifacts/{identity}.txt"
                await connection.execute(sa.insert(Artifact).values(
                    id=identity, work_item_id=work, name="복원 결과.txt", kind="evidence",
                    content_type="text/plain", object_key=key, size_bytes=len(content)))
                evidence[label] = (identity, work, key, content)
        root.mkdir(mode=0o700)
        for _, _, key, content in evidence.values():
            write_new(root, Path(key), content)
        return evidence
    finally:
        await engine.dispose()


@dataclass
class ArtifactRestoreRuntime:
    directory: Path
    api_url: str
    cookie_name: str
    seed: RestoreSeed
    root: Path
    snapshot: Path
    dump: Path
    manifest_sha256: str
    evidence: dict = field(repr=False)
    environment: dict = field(repr=False)
    client: httpx.Client = field(repr=False)
    process: subprocess.Popen | None = field(default=None, repr=False)
    logs: list[Path] = field(default_factory=list, repr=False)

    def cli(self, command, *args, expected=0, environment=None):
        common = ["--database-dump", str(self.dump)]
        if command != "create":
            common += ["--manifest-sha256", self.manifest_sha256]
        result = subprocess.run([sys.executable, "-m", "app.artifact_backup_admin",
            command, *common, *args], cwd=self.directory,
            env=self.environment | (environment or {}), capture_output=True, timeout=20)
        assert result.returncode == expected, "owned artifact command failed (output withheld)"
        for private in (self.seed.token.encode(), self.environment["DATABASE_URL"].encode(),
                        str(self.directory).encode(), TEXT):
            assert private not in result.stdout + result.stderr
        if expected:
            assert not result.stdout and b"Traceback" not in result.stderr
            return None
        return json.loads(result.stdout)

    def start(self):
        assert self.process is None or self.process.poll() is not None
        log_path = self.directory / f"api-{len(self.logs)}.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.logs.append(log_path)
        with os.fdopen(descriptor, "wb") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", self.api_url.rsplit(":", 1)[1], "--no-access-log", "--no-proxy-headers"],
                cwd=self.directory, env=self.environment, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert self.process.poll() is None, "owned restored API exited (log withheld)"
            try:
                if self.client.get("/readyz").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise AssertionError("owned restored API readiness timed out")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def restore_files(self):
        # Stop even this SELECT-only API before installing bytes; a failure leaves it offline.
        self.stop()
        result = self.cli("restore", "--backup", str(self.snapshot), "--output", str(self.root),
                          "--writers-stopped")
        assert self.cli("verify-restored")["verified"]
        self.start()
        return result


@contextmanager
def artifact_restore_runtime(directory: Path, *, port=None, web_origin="http://localhost:3000"):
    with restore_drill(directory) as drill:
        source = drill.create_database()
        drill.migrate(source)
        source_url = drill.database_url(source)
        seed = asyncio.run(seed_database(source_url))
        source_root = directory / "source-objects"
        evidence = asyncio.run(seed_files(source_url, source_root, seed))
        reader = drill.create_reader(source)
        source_reader = reader_url(drill, source, reader)
        before = asyncio.run(fingerprint(source_url))
        dump = drill.backup(source)
        target = drill.create_database()
        assert drill.restore(target, dump).returncode == 0, "owned database restore failed"
        target_url = drill.database_url(target)
        assert asyncio.run(fingerprint(target_url)) == before
        target_reader = sa.make_url(source_reader).set(database=target).render_as_string(
            hide_password=False)
        if port is None:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
        api_url = f"http://localhost:{port}"
        cookie = f"artifact_restore_{uuid.uuid4().hex}"
        root = directory / "restored-objects"
        environment = {
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "apps/api"),
            "DATABASE_URL": target_reader, "DATABASE_SCHEMA_MODE": "validate", "AUTH_MODE": "oidc",
            "OIDC_ISSUER_URL": ISSUER, "OIDC_CLIENT_ID": "artifact-restore-drill",
            "OIDC_REDIRECT_URI": f"{ISSUER}/auth/callback", "OIDC_SESSION_COOKIE_NAME": cookie,
            "DASHBOARD_URL": ISSUER, "CORS_ORIGINS": web_origin, "ARTIFACT_ROOT": str(root),
        }
        with httpx.Client(base_url=api_url, timeout=3, trust_env=False) as client:
            runtime = ArtifactRestoreRuntime(directory, api_url, cookie, seed, root,
                directory / "snapshot", dump, "", evidence, environment, client)
            created = runtime.cli("create", "--output", str(runtime.snapshot), "--writers-stopped",
                environment={"DATABASE_URL": source_reader, "ARTIFACT_ROOT": str(source_root)})
            runtime.manifest_sha256 = created["manifest_sha256"]
            assert created["verified"] and created["artifacts"] == 3
            assert runtime.cli("verify", "--backup", str(runtime.snapshot))["verified"]
            client.cookies.set(cookie, seed.token)
            try:
                runtime.start()
                yield runtime
            finally:
                runtime.stop()
                try:
                    for path in runtime.logs:
                        log = path.read_text()
                        assert seed.token not in log and target_reader not in log
                        assert_stream_log_clean(log)
                finally:
                    for path in runtime.logs:
                        path.unlink()
        assert asyncio.run(fingerprint(source_url)) == before
        assert asyncio.run(fingerprint(target_url)) == before
        for _, _, key, content in evidence.values():
            assert (source_root / key).read_bytes() == content
