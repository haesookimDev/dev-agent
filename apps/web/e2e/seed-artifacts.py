"""Upload synthetic evidence through the owned browser suite's real scoped worker."""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api" / "tests"))
from artifact_content_runtime import MARKUP, png_evidence


def seed(token_file):
    headers = {"Authorization": f"Bearer {Path(token_file).read_text().strip()}"}
    with httpx.Client(base_url="http://127.0.0.1:18100", trust_env=False, timeout=10) as client:
        response = client.post("/api/workers/register", headers=headers, json={
            "name": "browser-test-worker", "cpu_total": 2, "memory_mb_total": 4096,
            "disk_gb_available": 30, "labels": {"virtualization": "mock"},
        })
        assert response.status_code == 200, "synthetic worker registration failed"
        worker = response.json()["id"]
        response = client.post("/api/work-items", json={
            "title": "Artifact preview acceptance", "repository": "demo/artifact-preview",
            "requirement": "Inspect synthetic evidence; this fixture does not execute a VM.",
        })
        assert response.status_code == 201, "synthetic work creation failed"
        work = response.json()["id"]
        response = client.post(f"/api/workers/{worker}/claim", headers=headers,
                               json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
        assert response.status_code == 200, "synthetic claim failed"
        claim = response.json()
        assert claim["work_item"]["id"] == work
        lease = {"X-Kelpie-Lease": claim["lease_token"]}
        for name, content_type, content in [
            ("검증 결과 ✅.txt", "text/plain", MARKUP),
            ("result.json", "application/json", b'{"result":"synthetic evidence"}'),
            ("evidence.png", "image/png", png_evidence()),
        ]:
            response = client.post(f"/api/runs/{work}/artifacts/upload", headers=lease,
                params={"name": name, "content_type": content_type}, content=content)
            assert response.status_code == 201, "synthetic upload failed"
        response = client.post(f"/api/runs/{work}/artifacts", headers=lease, json={
            "kind": "evidence", "name": "missing.txt", "content_type": "text/plain",
            "size_bytes": 12, "object_key": f"{work}/artifacts/missing.txt",
        })
        assert response.status_code == 201, "synthetic missing-file registration failed"
        response = client.post(f"/api/runs/{work}/transition", headers=lease, json={
            "status": "failed", "expected_version": claim["work_item"]["version"],
            "message": "Synthetic evidence fixture finished; no VM execution.",
        })
        assert response.status_code == 200, "synthetic fixture transition failed"
        assert client.post(f"/api/runs/{work}/release", headers=lease).status_code == 204


if __name__ == "__main__":
    seed(sys.argv[1])
