from .models import WorkStatus

ALLOWED_TRANSITIONS: dict[WorkStatus, set[WorkStatus]] = {
    WorkStatus.QUEUED: {WorkStatus.PROVISIONING, WorkStatus.CANCELLED},
    WorkStatus.PROVISIONING: {
        WorkStatus.ANALYZING,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    },
    WorkStatus.ANALYZING: {
        WorkStatus.IMPLEMENTING,
        WorkStatus.AWAITING_INPUT,
        WorkStatus.BUDGET_EXHAUSTED,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    },
    WorkStatus.IMPLEMENTING: {
        WorkStatus.VERIFYING,
        WorkStatus.AWAITING_INPUT,
        WorkStatus.BUDGET_EXHAUSTED,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    },
    WorkStatus.VERIFYING: {
        WorkStatus.IMPLEMENTING,
        WorkStatus.AWAITING_FEEDBACK,
        WorkStatus.AWAITING_APPROVAL,
        WorkStatus.BUDGET_EXHAUSTED,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    },
    WorkStatus.AWAITING_FEEDBACK: {
        WorkStatus.IMPLEMENTING,
        WorkStatus.AWAITING_APPROVAL,
        WorkStatus.CANCELLED,
    },
    WorkStatus.AWAITING_APPROVAL: {
        WorkStatus.IMPLEMENTING,
        WorkStatus.COMMITTING,
        WorkStatus.CANCELLED,
    },
    WorkStatus.AWAITING_INPUT: {WorkStatus.IMPLEMENTING, WorkStatus.CANCELLED},
    WorkStatus.BUDGET_EXHAUSTED: {WorkStatus.IMPLEMENTING, WorkStatus.CANCELLED},
    WorkStatus.COMMITTING: {WorkStatus.PR_CREATED, WorkStatus.FAILED},
    WorkStatus.PR_CREATED: {WorkStatus.COMPLETED, WorkStatus.FAILED},
    WorkStatus.COMPLETED: set(),
    WorkStatus.FAILED: {WorkStatus.QUEUED, WorkStatus.CANCELLED},
    WorkStatus.CANCELLED: set(),
}


class InvalidTransition(ValueError):
    pass


def ensure_transition(current: WorkStatus, target: WorkStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition from {current.value} to {target.value}")
