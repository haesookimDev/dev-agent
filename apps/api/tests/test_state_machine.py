import pytest

from app.models import WorkStatus
from app.state_machine import InvalidTransition, ensure_transition


def test_valid_full_delivery_path() -> None:
    path = [
        WorkStatus.QUEUED,
        WorkStatus.PROVISIONING,
        WorkStatus.ANALYZING,
        WorkStatus.IMPLEMENTING,
        WorkStatus.VERIFYING,
        WorkStatus.AWAITING_APPROVAL,
        WorkStatus.COMMITTING,
        WorkStatus.PR_CREATED,
        WorkStatus.COMPLETED,
    ]
    for current, target in zip(path[:-1], path[1:], strict=True):
        ensure_transition(current, target)


def test_commit_cannot_skip_approval() -> None:
    with pytest.raises(InvalidTransition, match="cannot transition"):
        ensure_transition(WorkStatus.IMPLEMENTING, WorkStatus.COMMITTING)


def test_completed_is_terminal() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(WorkStatus.COMPLETED, WorkStatus.QUEUED)
