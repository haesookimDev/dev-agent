from api_event_runtime import assert_no_credentials, exercise_api_events
from artifact_runtime import artifact_runtime


def test_raw_worker_http_keeps_credentials_out_of_events_and_live_stream(tmp_path):
    credentials = []

    def verify_log(log):
        assert_no_credentials(log.encode(), credentials)

    with artifact_runtime(tmp_path, verify_log=verify_log) as runtime:
        assert exercise_api_events(runtime, credentials) >= 6
        database = runtime.database
    assert_no_credentials(database.read_bytes(), credentials)
    assert not (tmp_path / "api.log").exists()
