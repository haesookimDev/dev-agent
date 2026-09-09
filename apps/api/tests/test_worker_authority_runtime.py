from api_event_runtime import assert_no_credentials
from artifact_runtime import artifact_runtime
from worker_authority_runtime import PrivateCredentials, exercise_authority


def test_fixture_credentials_are_hidden_in_failure_representations():
    assert "owned-fixture-value" not in repr(PrivateCredentials(["owned-fixture-value"]))


def test_actual_oidc_http_requires_user_control_before_worker_progress(tmp_path):
    credentials = PrivateCredentials()

    def verify_log(log):
        assert_no_credentials(log.encode(), credentials)

    with artifact_runtime(tmp_path, verify_log=verify_log) as runtime:
        credentials.extend([*runtime.tokens,
                            *(value["X-Kelpie-Lease"] for value in runtime.leases.values())])
        assert exercise_authority(runtime, credentials) == {
            "mock_status": "completed", "budget_status": "verifying",
        }
        database = runtime.database
    assert_no_credentials(database.read_bytes(), credentials)
