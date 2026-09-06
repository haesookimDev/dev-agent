"""Fail a real migration CLI against only an owned, live SQLite acceptance API."""

import os
import sqlite3
import subprocess
import sys

from artifact_runtime import CONTENT, ROOT


def refuse_downgrade(runtime):
    with sqlite3.connect(runtime.database) as connection:
        before = tuple(connection.iterdump())
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "apps/api/alembic.ini"),
         "downgrade", "base"],
        cwd=ROOT, env={"PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "apps/api"),
            "DATABASE_URL": f"sqlite+aiosqlite:///{runtime.database}"},
        capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    assert b"would destroy all Kelpie data" in result.stderr
    with sqlite3.connect(runtime.database) as connection:
        assert tuple(connection.iterdump()) == before
    own, foreign = runtime.clients
    url = f"/api/work-items/{runtime.works[0]}/artifacts/{runtime.artifacts[0]}"
    response = own.get(url)
    assert response.status_code == 200 and response.content == CONTENT
    assert foreign.get(url).status_code == 404
    assert own.get("/readyz").status_code == 200
    return {"cli": "refused", "schema_and_rows": "unchanged", "ready": 200,
            "owned_artifact": 200, "other_organization": 404}
