from api_event_runtime import assert_no_credentials
from artifact_runtime import artifact_runtime
from transition_metadata_runtime import exercise_transition_metadata


def test_actual_http_and_feedback_preserve_authoritative_transition_history(tmp_path):
    credentials = []

    def verify_log(log):
        assert_no_credentials(log.encode(), credentials)

    with artifact_runtime(tmp_path, verify_log=verify_log) as runtime:
        credentials.extend([*runtime.tokens, runtime.leases[runtime.works[0]]["X-Kelpie-Lease"]])
        assert exercise_transition_metadata(runtime) == 9
        database = runtime.database
    assert_no_credentials(database.read_bytes(), credentials)
