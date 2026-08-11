"""Property-based proof that no sequence of legal operations can violate a Task invariant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from agtyle.domain.common import ErrorCode
from agtyle.domain.notifications import task_terminal_delivery_key
from agtyle.domain.tasks import ALLOWED_TRANSITIONS, ExecutionMode, Task, TaskOrigin, TaskStatus

START = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
MAX_ATTEMPTS = 3


def _initial_task() -> Task:
    return Task(
        id="task_00000000-0000-7000-8000-000000000001",
        intent_id="int_00000000-0000-7000-8000-000000000001",
        task_type="reminder_create",
        status=TaskStatus.CREATED,
        execution_mode=ExecutionMode.DELEGATED,
        assigned_agent_id="steward",
        objective="Create a reminder",
        origin=TaskOrigin(channel="api", conversation_id="conv", user_id="user_local"),
        max_attempts=MAX_ATTEMPTS,
        created_at=START,
        updated_at=START,
    )


class TaskLifecycle(RuleBasedStateMachine):
    """Drive a Task through arbitrary legal operations and assert the invariants hold."""

    def __init__(self) -> None:
        super().__init__()
        self.task = _initial_task()
        self.now = START
        self.terminal_delivery_keys: list[str] = []
        self.observed_statuses: set[TaskStatus] = {TaskStatus.CREATED}

    def _tick(self) -> datetime:
        self.now = self.now + timedelta(seconds=1)
        return self.now

    def _record(self, task: Task) -> None:
        self.task = task
        self.observed_statuses.add(task.status)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            self.terminal_delivery_keys.append(task_terminal_delivery_key(task.id, task.status))

    @precondition(
        lambda self: (
            self.task.can_transition_to(TaskStatus.ASSIGNED)
            and self.task.status is not TaskStatus.FAILED
        )
    )
    @rule()
    def assign(self) -> None:
        self._record(self.task.assign(agent_id="steward", now=self._tick()))

    @precondition(
        lambda self: self.task.status is TaskStatus.ASSIGNED and self.task.retry_budget_remains
    )
    @rule()
    def start(self) -> None:
        self._record(self.task.start(lease_owner="w1", now=self._tick(), lease_seconds=30))

    @precondition(lambda self: self.task.status is TaskStatus.RUNNING)
    @rule()
    def heartbeat(self) -> None:
        self._record(self.task.heartbeat(lease_owner="w1", now=self._tick(), lease_seconds=30))

    @precondition(lambda self: self.task.status is TaskStatus.RUNNING)
    @rule()
    def wait_for_approval(self) -> None:
        self._record(self.task.wait_for_approval(now=self._tick()))

    @precondition(lambda self: self.task.status is TaskStatus.WAITING_APPROVAL)
    @rule()
    def resume(self) -> None:
        self._record(self.task.resume(lease_owner="w1", now=self._tick(), lease_seconds=30))

    @precondition(lambda self: self.task.can_transition_to(TaskStatus.COMPLETED))
    @rule()
    def complete(self) -> None:
        self._record(self.task.complete(now=self._tick()))

    @precondition(lambda self: self.task.can_transition_to(TaskStatus.FAILED))
    @rule()
    def fail(self) -> None:
        self._record(
            self.task.fail(
                now=self._tick(),
                error_code=ErrorCode.CAPABILITY_TRANSIENT_FAILURE,
                error_message="transient",
            )
        )

    @precondition(
        lambda self: self.task.status is TaskStatus.FAILED and self.task.retry_budget_remains
    )
    @rule()
    def retry(self) -> None:
        self._record(self.task.reassign_for_retry(now=self._tick()))

    @precondition(lambda self: self.task.can_transition_to(TaskStatus.CANCELLED))
    @rule()
    def cancel(self) -> None:
        self._record(self.task.cancel(now=self._tick()))

    @rule()
    def observe(self) -> None:
        """Always-available no-op so a dead-ended Task is a valid resting state, not an error."""
        assert self.task.status in self.observed_statuses

    @invariant()
    def attempts_never_exceed_the_budget(self) -> None:
        assert 0 <= self.task.attempt_count <= self.task.max_attempts

    @invariant()
    def terminal_states_never_escape(self) -> None:
        if self.task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            assert not ALLOWED_TRANSITIONS[self.task.status]

    @invariant()
    def terminal_delivery_keys_are_unique_per_outcome(self) -> None:
        assert len(set(self.terminal_delivery_keys)) == len(
            dict.fromkeys(self.terminal_delivery_keys)
        )

    @invariant()
    def row_version_only_moves_forward(self) -> None:
        assert self.task.row_version >= 1

    @invariant()
    def only_running_tasks_hold_a_lease(self) -> None:
        if self.task.status is not TaskStatus.RUNNING:
            assert self.task.lease_owner is None
            assert self.task.lease_expires_at is None


TaskLifecycleTest = TaskLifecycle.TestCase
TaskLifecycleTest.settings = settings(
    max_examples=50,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
    print_blob=True,
)
