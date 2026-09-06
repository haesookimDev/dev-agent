from artifact_runtime import artifact_runtime
from stream_runtime_checks import assert_stream_log_clean, wait_state


def test_real_http_disconnect_during_sqlite_queries_returns_every_connection(tmp_path):
    with artifact_runtime(tmp_path, app_target="stream_runtime_app:app",
                          verify_log=assert_stream_log_clean) as runtime:
        own, foreign = runtime.clients
        url = f"/api/work-items/{runtime.works[0]}/events"
        for count in range(1, 9):
            assert own.post("/__test/arm-stream-pause").status_code == 200
            with own.stream("GET", url) as response:
                assert response.status_code == 200
                wait_state(own, lambda state, count=count:
                           state["paused"] and state["pauses"] == count)
                # Close the actual socket while SQLite is still executing its paused query.
            release = own.post("/__test/release-stream-pause")
            assert release.status_code == 200 and release.json()["was_paused"] is True
            state = wait_state(own, lambda state: state["active"] == state["checked_out"] == 0)
            assert state["started"] == state["closed"] == count
        with own.stream("GET", url) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("data:"):
                    assert "work.created" in line
                    break
            else:
                raise AssertionError("A fresh stream must still deliver real events")
        wait_state(own, lambda state: state["active"] == state["checked_out"] == 0)
        assert foreign.get(url).status_code == 404
