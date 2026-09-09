"""Owned browser fixture: real scoped Worker HTTP, never a VM or model session."""

import json
import os
import sys
from pathlib import Path
from uuid import UUID

import httpx


def exercise(metadata_file, action, work_id=None):
    metadata_path = Path(metadata_file)
    metadata = json.loads(metadata_path.read_text())
    assert metadata["api"] in ("http://127.0.0.1:18100", "http://localhost:18500")
    token = Path(metadata["token_file"]).read_text().strip()
    with httpx.Client(base_url=metadata["api"], timeout=10, trust_env=False) as client:
        if action == "create":
            headers = {"Authorization": f"Bearer {token}"}
            registered = client.post("/api/workers/register", headers=headers, json={
                "name": "browser-budget-worker", "cpu_total": 2, "memory_mb_total": 4096,
                "disk_gb_available": 30, "labels": {"virtualization": "mock"},
            })
            assert registered.status_code == 200, "owned budget worker registration failed"
            created = client.post("/api/work-items", json={
                "title": "시간 예산 연장 / Review additional development time",
                "repository": "demo/budget-approval", "budget_minutes": 30,
                "requirement": "Synthetic scoped Worker fixture; no VM or model execution.",
            })
            assert created.status_code == 201, "owned budget work creation failed"
            work_id = created.json()["id"]
            claimed = client.post(f"/api/workers/{registered.json()['id']}/claim",
                headers=headers, json={"cpu": 2, "memory_mb": 4096, "disk_gb": 30})
            assert claimed.status_code == 200, "owned budget claim failed"
            assert claimed.json()["work_item"]["id"] == work_id, "unexpected queued work claimed"
            state = {"lease": claimed.json()["lease_token"]}
            state_path = metadata_path.with_name(f"budget-{UUID(work_id)}.json")
            descriptor = os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as destination:
                json.dump(state, destination)
            targets = ("analyzing", "budget_exhausted")
        else:
            state_path = metadata_path.with_name(f"budget-{UUID(work_id)}.json")
            state = json.loads(state_path.read_text())
            targets = ("verifying", "budget_exhausted") if action == "exhaust" else ("failed",)
        lease = {"X-Kelpie-Lease": state["lease"]}
        for target in targets:
            before = client.get(f"/api/work-items/{work_id}")
            assert before.status_code == 200, "owned work lookup failed"
            changed = client.post(f"/api/runs/{work_id}/transition", headers=lease, json={
                "status": target, "expected_version": before.json()["version"],
                "message": "Synthetic browser fixture; no VM execution.",
            })
            assert changed.status_code == 200, f"owned transition to {target} failed"
        if action == "finish":
            released = client.post(f"/api/runs/{work_id}/release", headers=lease)
            assert released.status_code == 204, "owned budget lease release failed"
            state_path.unlink()
        return changed.json()


if __name__ == "__main__":
    assert len(sys.argv) in (3, 4) and sys.argv[2] in ("create", "exhaust", "finish")
    work = exercise(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    print(json.dumps({key: work[key] for key in ("id", "status", "version", "budget_minutes")}))
