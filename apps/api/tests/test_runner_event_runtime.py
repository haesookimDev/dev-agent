from artifact_runtime import artifact_runtime
from runner_event_runtime import assert_no_credentials, exercise_runner_events


def test_runner_http_and_verification_keep_credentials_out_of_retained_events(tmp_path):
    credentials = []

    def verify_log(log):
        assert_no_credentials(log.encode(), credentials)

    with artifact_runtime(tmp_path, verify_log=verify_log) as runtime:
        assert exercise_runner_events(runtime, tmp_path, credentials) >= 10
        database = runtime.database
    assert_no_credentials(database.read_bytes(), credentials)
    assert not list(tmp_path.glob("runner-probe-*"))
