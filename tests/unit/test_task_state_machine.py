"""Task state machine: every declared transition works and every other one is rejected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agtyle.domain.common import ErrorCode, IllegalTransitionError, RetryExhaustedError
from agtyle.domain.tasks import (
    ALLOWED_TRANSITIONS,
    ExecutionMode,
    Task,
    TaskOrigin,
    TaskStatus,
)

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)

ORIGIN = TaskOrigin(channel="api", conversation_id="conv_demo", user_id="user_local")


def make_task(status: TaskStatus = TaskStatus.CREATED, **overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "task_00000000-0000-7000-8000-000000000001",
        "intent_id": "int_00000000-0000-7000-8000-000000000001",
        "task_type": "reminder_create",
        "status": status,
        "execution_mode": ExecutionMode.DELEGATED,
        "assigned_agent_id": "steward",
        "objective": "Create a reminder",
        "payload": {},
        "origin": ORIGIN,
        "max_attempts": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def _apply(task: Task, target: TaskStatus) -> Task:
    """Drive one transition through the public domain method for the target status."""
    match target:
        case TaskStatus.CREATED:
            # `created` is an entry state only. No public method can return a Task to it.
            raise IllegalTransitionError("no public transition returns a task to created")
        case TaskStatus.ASSIGNED if task.status is TaskStatus.FAILED:
            return task.reassign_for_retry(now=NOW)
        case TaskStatus.ASSIGNED:
            return task.assign(agent_id="steward", now=NOW)
        case TaskStatus.RUNNING if task.status is TaskStatus.WAITING_APPROVAL:
            return task.resume(lease_owner="worker-1", now=NOW, lease_seconds=30)
        case TaskStatus.RUNNING:
            return task.start(lease_owner="worker-1", now=NOW, lease_seconds=30)
        case TaskStatus.WAITING_APPROVAL:
            return task.wait_for_approval(now=NOW)
        case TaskStatus.COMPLETED:
            return task.complete(now=NOW)
        case TaskStatus.FAILED:
            return task.fail(now=NOW, error_code=ErrorCode.INVALID_AGENT_OUTPUT, error_message="x")
        case TaskStatus.CANCELLED:
            return task.cancel(now=NOW)
        case _:  # pragma: no cover - exhaustive over TaskStatus
            raise AssertionError(target)


@pytest.mark.parametrize(
    ("source", "target"),
    [(source, target) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets],
)
def test_task_allows_every_declared_transition(source: TaskStatus, target: TaskStatus) -> None:
    task = make_task(source, attempt_count=1 if source is TaskStatus.FAILED else 0)
    moved = _apply(task, target)
    assert moved.status is target
    assert moved.row_version == task.row_version + 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in TaskStatus
        for target in TaskStatus
        if target not in ALLOWED_TRANSITIONS[source]
    ],
)
def test_task_rejects_every_undeclared_transition(source: TaskStatus, target: TaskStatus) -> None:
    task = make_task(source, attempt_count=0)
    with pytest.raises((IllegalTransitionError, RetryExhaustedError)):
        _apply(task, target)


@pytest.mark.parametrize("terminal", [TaskStatus.COMPLETED, TaskStatus.CANCELLED])
def test_terminal_task_cannot_reopen(terminal: TaskStatus) -> None:
    task = make_task(terminal)
    assert task.is_terminal
    for target in TaskStatus:
        with pytest.raises((IllegalTransitionError, RetryExhaustedError)):
            _apply(task, target)


def test_failed_task_reassign_requires_retry_budget() -> None:
    exhausted = make_task(TaskStatus.FAILED, attempt_count=3, max_attempts=3)
    with pytest.raises(RetryExhaustedError):
        exhausted.reassign_for_retry(now=NOW)

    retryable = make_task(TaskStatus.FAILED, attempt_count=1, max_attempts=3)
    assert retryable.reassign_for_retry(now=NOW).status is TaskStatus.ASSIGNED


def test_start_increments_attempt_and_sets_lease() -> None:
    task = make_task(TaskStatus.ASSIGNED)
    running = task.start(lease_owner="worker-1", now=NOW, lease_seconds=30)
    assert running.attempt_count == 1
    assert running.lease_owner == "worker-1"
    assert running.lease_expires_at == NOW + timedelta(seconds=30)
    assert running.owns_lease("worker-1", now=NOW)


def test_start_refuses_to_exceed_the_attempt_budget() -> None:
    task = make_task(TaskStatus.ASSIGNED, attempt_count=3, max_attempts=3)
    with pytest.raises(RetryExhaustedError):
        task.start(lease_owner="worker-1", now=NOW, lease_seconds=30)


def test_expired_lease_is_detected_and_old_owner_loses_ownership() -> None:
    running = make_task(TaskStatus.ASSIGNED).start(
        lease_owner="worker-1", now=NOW, lease_seconds=30
    )
    later = NOW + timedelta(seconds=31)
    assert running.lease_expired(now=later)
    assert not running.owns_lease("worker-1", now=later)


def test_heartbeat_requires_the_owning_worker() -> None:
    running = make_task(TaskStatus.ASSIGNED).start(
        lease_owner="worker-1", now=NOW, lease_seconds=30
    )
    renewed = running.heartbeat(
        lease_owner="worker-1", now=NOW + timedelta(seconds=10), lease_seconds=30
    )
    assert renewed.lease_expires_at == NOW + timedelta(seconds=40)
    with pytest.raises(IllegalTransitionError):
        running.heartbeat(lease_owner="worker-2", now=NOW, lease_seconds=30)


def test_interactive_conversion_keeps_the_same_task() -> None:
    task = make_task(TaskStatus.ASSIGNED, execution_mode=ExecutionMode.INTERACTIVE)
    converted = task.convert_to_delegated(now=NOW)
    assert converted.id == task.id
    assert converted.execution_mode is ExecutionMode.DELEGATED
    assert converted.status is TaskStatus.ASSIGNED
    assert converted.lease_owner is None


def test_interactive_conversion_of_a_running_task_requeues_the_same_task() -> None:
    """A claimed interactive attempt travels `running -> failed -> assigned`, keeping its id."""
    running = make_task(TaskStatus.ASSIGNED, execution_mode=ExecutionMode.INTERACTIVE).start(
        lease_owner="worker-1", now=NOW, lease_seconds=30
    )
    converted = running.convert_to_delegated(now=NOW)
    assert converted.id == running.id
    assert converted.status is TaskStatus.ASSIGNED
    assert converted.execution_mode is ExecutionMode.DELEGATED
    assert converted.lease_owner is None
    assert converted.attempt_count == 1
    assert converted.last_error_code is ErrorCode.AGENT_RUNTIME_TIMEOUT


def test_interactive_conversion_rejects_a_terminal_task() -> None:
    completed = (
        make_task(TaskStatus.ASSIGNED, execution_mode=ExecutionMode.INTERACTIVE)
        .start(lease_owner="worker-1", now=NOW, lease_seconds=30)
        .complete(now=NOW)
    )
    with pytest.raises(IllegalTransitionError):
        completed.convert_to_delegated(now=NOW)


def test_interactive_conversion_refuses_when_the_retry_budget_is_gone() -> None:
    exhausted = make_task(
        TaskStatus.ASSIGNED,
        execution_mode=ExecutionMode.INTERACTIVE,
        attempt_count=2,
        max_attempts=3,
    ).start(lease_owner="worker-1", now=NOW, lease_seconds=30)
    with pytest.raises(RetryExhaustedError):
        exhausted.convert_to_delegated(now=NOW)


def test_status_assignment_outside_domain_methods_is_impossible() -> None:
    task = make_task(TaskStatus.ASSIGNED)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen models
        task.status = TaskStatus.COMPLETED  # type: ignore[misc]
