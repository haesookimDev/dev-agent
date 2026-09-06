import sqlite3

from artifact_runtime import CONTENT, artifact_runtime
from stream_runtime_checks import assert_stream_log_clean


def test_real_http_artifact_recovery_and_authorization_keep_no_store_headers(tmp_path):
    with artifact_runtime(tmp_path, verify_log=assert_stream_log_clean) as runtime:
        own, foreign = runtime.clients
        work = runtime.works[0]
        listing = f"/api/work-items/{work}/artifacts"
        url = f"{listing}/{runtime.artifacts[0]}"
        request_ids = set()

        def check(response, status):
            assert response.status_code == status
            assert response.headers.get("cache-control") == "no-store"
            assert "origin" in response.headers["vary"].lower().split(", ")
            identity = response.headers["x-request-id"]
            assert identity not in request_ids
            request_ids.add(identity)
            if status != 200:
                assert CONTENT not in response.content
                assert "owned-evidence.txt" not in response.text
            return response

        own.headers.pop("Origin")
        downloaded = check(own.get(url), 200)
        assert downloaded.content == CONTENT
        assert downloaded.headers["content-security-policy"] == "sandbox"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        path = runtime.root / runtime.key(runtime.artifacts[0])
        held = tmp_path / "held-evidence"
        path.rename(held)
        check(own.get(url), 410)
        held.rename(path)
        assert check(own.get(url), 200).content == CONTENT
        own.headers["Origin"] = "http://localhost:3000"
        allowed = check(own.get(url), 200)
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
        for target in (listing, url):
            check(own.get(target), 200)
            check(foreign.get(target), 404)
        own.cookies.clear()
        for target in (listing, url):
            check(own.get(target), 401)
        own.cookies.set(runtime.cookie_name, runtime.tokens[0])
        with sqlite3.connect(runtime.database) as connection:
            connection.execute("DELETE FROM memberships WHERE organization_id=?", ("artifact-0",))
        for target in (listing, url):
            check(own.get(target), 403)
