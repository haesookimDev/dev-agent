import sqlite3

from artifact_runtime import CONTENT, artifact_runtime


def test_real_http_work_scoping_and_retained_links(tmp_path):
    with artifact_runtime(tmp_path) as runtime:
        own, foreign = runtime.clients
        work, other_work = runtime.works
        artifact, other_artifact = runtime.artifacts
        url = f"/api/work-items/{work}/artifacts/{artifact}"
        assert own.get(url).content == CONTENT
        assert foreign.get(url).status_code == 404
        assert own.get(f"/api/work-items/{other_work}").status_code == 404
        assert {item["id"] for item in own.get("/api/work-items").json()} == {work}
        with sqlite3.connect(runtime.database) as connection:
            before = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        response = own.post(f"/api/runs/{work}/artifacts", headers=runtime.leases[work], json={
            "kind": "evidence", "name": "forbidden.txt", "content_type": "text/plain",
            "object_key": runtime.key(other_artifact), "size_bytes": len(CONTENT),
        })
        assert response.status_code == 422
        assert response.json() == {"detail": "artifact key must belong to this work"}
        with sqlite3.connect(runtime.database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == before
        retained = runtime.retain_alias(runtime.key(other_artifact))
        denied = own.get(f"/api/work-items/{work}/artifacts/{retained}")
        assert denied.status_code == 410
        assert denied.json() == {"detail": "artifact content is unavailable"}
        path = runtime.root / runtime.key(artifact)
        path.unlink()
        path.symlink_to(runtime.root / runtime.key(other_artifact))
        assert own.get(url).status_code == 410
        path.parent.rename(path.parent.with_name("original-artifacts"))
        path.parent.symlink_to((runtime.root / runtime.key(other_artifact)).parent,
                               target_is_directory=True)
        files = set(path.parent.iterdir())
        rejected = own.post(f"/api/runs/{work}/artifacts/upload", headers=runtime.leases[work],
            params={"name": "rejected.txt", "content_type": "text/plain"}, content=CONTENT)
        assert rejected.status_code == 503
        assert rejected.json() == {"detail": "artifact storage is unavailable"}
        assert set(path.parent.iterdir()) == files
