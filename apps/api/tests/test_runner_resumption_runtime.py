import json
import sqlite3
from pathlib import Path

import pytest
from api_event_runtime import assert_no_credentials
from artifact_runtime import artifact_runtime
from runner_resumption_runtime import running_runner
from worker_authority_runtime import PrivateCredentials, admin_client


@pytest.mark.parametrize("with_feedback", [False, True])
def test_actual_runner_waits_for_budget_and_resumes_user_decisions(tmp_path, with_feedback):
    credentials = PrivateCredentials()
    with artifact_runtime(tmp_path, verify_log=lambda log:
                          assert_no_credentials(log.encode(), credentials)) as runtime:
        credentials.extend([*runtime.tokens,
                            *(headers["X-Kelpie-Lease"] for headers in runtime.leases.values())])
        own, foreign = runtime.clients
        work_url = f"/api/work-items/{runtime.works[0]}"
        with (admin_client(runtime, 0, credentials) as admin,
              running_runner(runtime, tmp_path / "runner", credentials) as runner):
            runner.wait(lambda: runner.work()["status"] == "budget_exhausted")
            before = runner.work()
            if with_feedback:
                response = own.post(f"{work_url}/feedback", json={"message": "Keep this revision"})
                assert response.status_code == 200
                assert response.json()["status"] == "budget_exhausted"
            budget = {"kind": "budget", "decision": "approve", "payload": {"minutes": 15}}
            assert own.post(f"{work_url}/approvals", json=budget).status_code == 403
            assert foreign.post(f"{work_url}/approvals", json=budget).status_code == 404
            observed = len(runner.events("fixture.commands.observed"))
            runner.wait(lambda: len(runner.events("fixture.commands.observed")) >= observed + 2)
            assert len(runner.events("fixture.turn.completed")) == 1
            assert runner.work()["status"] == "budget_exhausted"
            assert runner.work()["version"] == before["version"]
            assert runner.events("fixture.commands.observed")[-1]["payload"]["after_feedback"] == 0
            response = admin.post(f"{work_url}/approvals", json=budget)
            assert response.status_code == 200 and response.json()["status"] == "implementing"
            assert response.json()["budget_minutes"] == before["budget_minutes"] + 15
            runner.wait(lambda: runner.work()["status"] == "awaiting_approval")
            turns = runner.events("fixture.turn.completed")
            assert [turn["payload"]["turn"] for turn in turns] == [1, 2]
            assert "owned verification failure" in turns[-1]["payload"]["prompt"]
            if with_feedback:
                assert "Keep this revision" in turns[-1]["payload"]["prompt"]
            revision = runner.work()["version"]
            rejected = admin.post(f"{work_url}/approvals", json={
                "kind": "pull_request", "decision": "reject",
            })
            assert rejected.status_code == 200 and rejected.json()["status"] == "implementing"
            runner.wait(lambda: runner.work()["status"] == "awaiting_approval"
                        and runner.work()["version"] > revision)
            assert len(runner.events("fixture.turn.completed")) == 3
            accepted = admin.post(f"{work_url}/approvals", json={
                "kind": "pull_request", "decision": "approve",
            })
            assert accepted.status_code == 200 and accepted.json()["status"] == "committing"
            assert runner.process.wait(timeout=10) == 0
            assert len(runner.events("delivery.ready")) == 1
            assert len(runner.events("fixture.session.closed")) == 1
            assert not runner.events("runner.failed")
            checks = runner.events("verification.completed")
            assert [check["payload"]["exit_code"] for check in checks] == [1, 0, 0]
            response = admin.get(f"{work_url}/audit-log")
            assert response.status_code == 200
            approvals = [row for row in response.json() if row["action"] == "approval.decided"]
            assert [row["details"]["decision"] for row in approvals] == [
                "approve", "reject", "approve",
            ]
            assert all(row["actor_subject"] == "admin-0" for row in approvals)
            with sqlite3.connect(runtime.database) as database:
                bundle = database.execute(
                    "SELECT object_path FROM delivery_bundles WHERE work_item_id = ?",
                    (runtime.works[0],),
                ).fetchone()
                assert bundle is not None
                path = Path(bundle[0]).resolve()
                assert path.is_relative_to(runtime.root.resolve())
                assert b"fixed revision 3" in path.read_bytes()
                assert database.execute("SELECT count(*) FROM delivery_jobs").fetchone()[0] == 0
            events = own.get(f"{work_url}/event-log")
            assert events.status_code == 200
            assert_no_credentials(json.dumps(events.json()).encode(), credentials)
        database_path = runtime.database
    assert_no_credentials(database_path.read_bytes(), credentials)
