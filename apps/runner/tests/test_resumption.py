import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kelpie_runner import main


class Lifecycle:
    """Script control-plane decisions; never launch an agent, git or a network client."""

    def __init__(self, checks, polls):
        self.state, self.version = "analyzing", 2
        self.checks, self.polls = iter(checks), iter(polls)
        self.turns, self.transitions, self.cursors, self.events = [], [], [], []
        self.bundles = 0
        self.closed = False

    async def transition(self, target, version, message):
        assert version == self.version
        allowed = {"analyzing": {"implementing"}, "implementing": {"verifying"},
                   "verifying": {"implementing", "awaiting_approval", "budget_exhausted"}}
        assert target in allowed.get(self.state, set()), "Runner attempted a platform-only edge"
        self.transitions.append((self.state, target))
        self.state, self.version = target, self.version + 1
        return {"status": self.state, "version": self.version}

    async def commands(self, feedback_cursor, approval_cursor):
        self.cursors.append((feedback_cursor, approval_cursor))
        step = next(self.polls, None)
        assert step is not None, "Runner did not settle the scripted user decision"
        state, feedback, approvals = step
        if state != self.state:
            self.state, self.version = state, self.version + 1
        return {"status": self.state, "version": self.version,
                "feedback": feedback, "approvals": approvals}

    async def event(self, event_type, message, **kwargs):
        self.events.append(event_type)

    async def upload_delivery_bundle(self, content):
        assert self.state == "verifying"
        self.bundles += 1

    async def close(self):
        self.closed = True

    async def run_turn(self, prompt):
        self.turns.append((self.state, prompt))
        assert self.state == "implementing", "Runner executed without control-plane resumption"

    async def verify(self, repository, control):
        assert self.state == "verifying"
        result = next(self.checks)
        if isinstance(result, Exception):
            raise result
        return result, "checks passed" if result else "owned verification failure"


@pytest.fixture
def exercise(monkeypatch, tmp_path):
    def execute(checks, polls, replan_limit=0):
        control = Lifecycle(checks, polls)
        session = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), run_turn=control.run_turn)
        assignment = main.Assignment("owned-work", "owned-correlation", "Owned work",
                                     "Revise the fixture", "example/fixture", 2, 60, replan_limit)
        monkeypatch.setenv("KELPIE_CONTROL_URL", "http://runner.invalid")
        monkeypatch.setenv("KELPIE_LEASE_TOKEN", "owned-noncredential-placeholder")
        monkeypatch.setenv("KELPIE_WORK_ROOT", str(tmp_path))
        monkeypatch.setattr(main.Assignment, "from_environment", lambda: assignment)
        monkeypatch.setattr(main, "ControlClient", lambda *args: control)
        monkeypatch.setattr(main, "CodexAppServer", lambda *args: session)
        monkeypatch.setattr(main, "clone_repository", AsyncMock(return_value=tmp_path))
        monkeypatch.setattr(main, "run_verification", control.verify)
        monkeypatch.setattr(main, "upload_evidence", AsyncMock())
        monkeypatch.setattr(main, "create_delivery_bundle", AsyncMock(return_value=b"owned patch"))
        monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
        try:
            asyncio.run(main.run())
        finally:
            assert control.closed
            session.close.assert_awaited_once()
        return control
    return execute


FEEDBACK = [{"id": 5, "message": "Preserve the user's revision request"}]
COMMITTING = ("committing", [], [])


@pytest.mark.parametrize("waiting", [
    "budget_exhausted", "awaiting_input", "awaiting_feedback", "awaiting_approval", "future_wait",
])
def test_feedback_waits_for_authorized_state_and_keeps_cursor(exercise, waiting):
    approval = [{"id": 9, "kind": "budget", "decision": "approve"}]
    control = exercise([waiting != "budget_exhausted", True], [
        (waiting, FEEDBACK, approval), (waiting, FEEDBACK, []),
        ("implementing", FEEDBACK, []), COMMITTING,
    ])
    assert [state for state, _ in control.turns] == ["implementing", "implementing"]
    assert FEEDBACK[0]["message"] in control.turns[-1][1]
    assert control.cursors == [(0, 0), (0, 9), (0, 9), (5, 9)]
    assert control.events[-1] == "delivery.ready"


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed", "pr_created", "committing"])
def test_terminal_or_delivery_state_precedes_stale_feedback(exercise, status):
    control = exercise([True], [(status, FEEDBACK, [])])
    assert len(control.turns) == 1
    assert control.bundles == 1
    assert ("delivery.ready" in control.events) == (status == "committing")


@pytest.mark.parametrize("kind,decision,initial_pass", [
    ("budget", "approve", False), ("pull_request", "reject", True),
])
def test_user_decision_resumes_without_feedback(exercise, kind, decision, initial_pass):
    control = exercise([initial_pass, True], [
        ("implementing", [], [{"id": 7, "kind": kind, "decision": decision}]), COMMITTING,
    ])
    assert len(control.turns) == 2
    assert control.cursors == [(0, 0), (0, 7)]
    assert control.bundles == 1 + int(initial_pass)
    if not initial_pass:
        assert "owned verification failure" in control.turns[-1][1]


def test_authorized_state_does_not_require_replaying_consumed_approval(exercise):
    control = exercise([False, True], [("implementing", [], []), COMMITTING])
    assert len(control.turns) == 2
    assert "owned verification failure" in control.turns[-1][1]


def test_feedback_resume_keeps_independent_verification_and_delivery(exercise):
    control = exercise([True, True], [("implementing", FEEDBACK, []), COMMITTING])
    assert len(control.turns) == 2
    assert control.bundles == 2
    assert control.cursors == [(0, 0), (5, 0)]


def test_repeated_budget_approval_preserves_bounded_repairs_and_failure_context(exercise):
    approved = [{"id": 1, "kind": "budget", "decision": "approve"}]
    control = exercise([False, False, False, False, True], [
        ("budget_exhausted", FEEDBACK, []), ("implementing", FEEDBACK, approved),
        ("budget_exhausted", [], []), ("implementing", [], []), COMMITTING,
    ], replan_limit=1)
    assert len(control.turns) == 5
    assert control.transitions.count(("verifying", "budget_exhausted")) == 2
    assert control.bundles == 1
    assert "owned verification failure" in control.turns[-1][1]


def test_verification_exception_still_fails_and_closes_resources(exercise):
    with pytest.raises(RuntimeError, match="owned verifier failure"):
        exercise([True, RuntimeError("owned verifier failure")], [
            ("implementing", FEEDBACK, []),
        ])


def test_initial_success_waits_without_starting_another_turn(exercise):
    control = exercise([True], [("awaiting_approval", [], []), COMMITTING])
    assert len(control.turns) == control.bundles == 1
