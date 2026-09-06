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
