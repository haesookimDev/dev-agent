"""Real upload -> terminal lease release -> CLI retention, in the owned browser-suite DB."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import sqlalchemy as sa
from app.models import Artifact, WorkItem, utcnow
from sqlalchemy.ext.asyncio import create_async_engine


def seed(token_file):
    headers = {"Authorization": f"Bearer {Path(token_file).read_text().strip()}"}
    with httpx.Client(base_url="http://127.0.0.1:18100", trust_env=False, timeout=10) as client:
        response = client.post("/api/workers/register", headers=headers, json={
            "name": "browser-test-worker", "cpu_total": 2, "memory_mb_total": 4096,
            "disk_gb_available": 30, "labels": {"virtualization": "mock"}})
        assert response.status_code == 200, "synthetic retention worker registration failed"
        worker = response.json()["id"]
        response = client.post("/api/work-items", json={
            "title": "Artifact retention acceptance", "repository": "demo/artifact-retention",
            "requirement": "Inspect expired evidence without starting a VM."})
        assert response.status_code == 201, "synthetic retention work creation failed"
        work = response.json()["id"]
        response = client.post(f"/api/workers/{worker}/claim", headers=headers,
                              json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
        assert response.status_code == 200, "synthetic retention claim failed"
        claim = response.json()
        assert claim["work_item"]["id"] == work
        lease = {"X-Kelpie-Lease": claim["lease_token"]}
        response = client.post(f"/api/runs/{work}/artifacts/upload", headers=lease,
            params={"name": "retained-evidence.txt", "content_type": "text/plain"},
            content=b"Synthetic expired evidence\n")
        assert response.status_code == 201, "synthetic retention upload failed"
        identity = response.json()["id"]
        url = f"/api/work-items/{work}/artifacts/{identity}"
        assert client.get(url).status_code == 200
        response = client.post(f"/api/runs/{work}/transition", headers=lease, json={
            "status": "cancelled", "expected_version": claim["work_item"]["version"],
            "message": "Synthetic retention fixture finished; no VM execution."})
        assert response.status_code == 200, "synthetic retention transition failed"
        assert client.post(f"/api/runs/{work}/release", headers=lease).status_code == 204

        async def age_owned_evidence():
            engine = create_async_engine(os.environ["DATABASE_URL"])
            try:
                async with engine.begin() as connection:
                    past = utcnow() - timedelta(days=40)
                    await connection.execute(sa.update(WorkItem).where(WorkItem.id == work)
                                             .values(updated_at=past))
                    await connection.execute(sa.update(Artifact).where(Artifact.id == identity)
                                             .values(created_at=past))
            finally:
                await engine.dispose()
        asyncio.run(age_owned_evidence())
        result = subprocess.run([sys.executable, "-m", "app.artifact_retention_admin",
            "--retain-days", "30", "--work-id", work, "--apply"],
            capture_output=True, timeout=15, check=False)
        assert result.returncode == 0, "synthetic retention CLI failed (output withheld)"
        assert json.loads(result.stdout)["counts"]["purged"] == 1
        assert client.get(url).json() == {"detail": "artifact retention period has expired"}
        assert client.get(url).status_code == 410


if __name__ == "__main__":
    seed(sys.argv[1])
