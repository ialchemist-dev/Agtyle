"""Task: the unit of durable work. Tasks drive execution; Events never do."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field, StringConstraints

from agtyle.domain.common import (
    AgentId,
    DomainModel,
    ErrorCode,
    IllegalTransitionError,
    IntentId,
    JsonMapping,
    RetryExhaustedError,
    TaskId,
    UserId,
    UtcDatetime,
    require_aware,
)


class TaskStatus(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    DELEGATED = "delegated"
    APPROVAL_GATED = "approval_gated"


TERMINAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
)

ALLOWED_TRANSITIONS: Final[dict[TaskStatus, frozenset[TaskStatus]]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.ASSIGNED, TaskStatus.CANCELLED}),
    TaskStatus.ASSIGNED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    # A failed Task may only return to `assigned`, and only when retry budget remains.
    TaskStatus.FAILED: frozenset({TaskStatus.ASSIGNED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class TaskOrigin(DomainModel):
    """Where the Task came from, so notifications can be routed back to the user."""

    channel: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    conversation_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    user_id: UserId


class Task(DomainModel):
    """A durable unit of assigned work with an explicit state machine and lease."""

    id: TaskId
    intent_id: IntentId
    task_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: TaskStatus
    execution_mode: ExecutionMode
    assigned_agent_id: AgentId
    objective: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    payload: JsonMapping = Field(default_factory=dict)
    origin: TaskOrigin
    attempt_count: Annotated[int, Field(ge=0)] = 0
    max_attempts: Annotated[int, Field(ge=1)]
    lease_owner: str | None = None
    lease_expires_at: UtcDatetime | None = None
    next_attempt_at: UtcDatetime | None = None
    last_error_code: ErrorCode | None = None
    last_error_message: str | None = None
    row_version: Annotated[int, Field(ge=1)] = 1
    created_at: UtcDatetime
    updated_at: UtcDatetime

    # -- state machine ---------------------------------------------------------------

    def can_transition_to(self, target: TaskStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.status]

    def _transition(self, target: TaskStatus, *, now: datetime, **changes: object) -> Task:
        if not self.can_transition_to(target):
            raise IllegalTransitionError(
                f"task cannot move from {self.status.value} to {target.value}",
                task_id=self.id,
            )
        return self.model_copy(
            update={
                "status": target,
                "updated_at": require_aware(now, field="now"),
                "row_version": self.row_version + 1,
                **changes,
            }
        )

    def assign(self, *, agent_id: str, now: datetime) -> Task:
        return self._transition(TaskStatus.ASSIGNED, now=now, assigned_agent_id=agent_id)

    def start(self, *, lease_owner: str, now: datetime, lease_seconds: int) -> Task:
        """Claim the Task for one attempt. The caller must hold the write transaction."""
        if self.attempt_count >= self.max_attempts:
            raise RetryExhaustedError(
                f"task already used {self.attempt_count} of {self.max_attempts} attempts",
                task_id=self.id,
            )
        moment = require_aware(now, field="now")
        return self._transition(
            TaskStatus.RUNNING,
            now=moment,
            lease_owner=lease_owner,
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
            attempt_count=self.attempt_count + 1,
            next_attempt_at=None,
        )

    def heartbeat(self, *, lease_owner: str, now: datetime, lease_seconds: int) -> Task:
        """Renew the lease. Only the owning Worker may renew."""
        if self.status is not TaskStatus.RUNNING:
            raise IllegalTransitionError("only a running task can heartbeat", task_id=self.id)
        if self.lease_owner != lease_owner:
            raise IllegalTransitionError("lease owner mismatch", task_id=self.id)
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )

    def wait_for_approval(self, *, now: datetime) -> Task:
        """Park the Task for a human decision and release the Worker's lease."""
        return self._transition(
            TaskStatus.WAITING_APPROVAL, now=now, lease_owner=None, lease_expires_at=None
        )

    def resume(self, *, lease_owner: str, now: datetime, lease_seconds: int) -> Task:
        moment = require_aware(now, field="now")
        return self._transition(
            TaskStatus.RUNNING,
            now=moment,
            lease_owner=lease_owner,
            lease_expires_at=moment + timedelta(seconds=lease_seconds),
        )

    def complete(self, *, now: datetime) -> Task:
        return self._transition(
            TaskStatus.COMPLETED,
            now=now,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error_code=None,
            last_error_message=None,
        )

    def fail(
        self,
        *,
        now: datetime,
        error_code: ErrorCode,
        error_message: str,
        next_attempt_at: datetime | None = None,
    ) -> Task:
        return self._transition(
            TaskStatus.FAILED,
            now=now,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=error_code,
            last_error_message=error_message,
            next_attempt_at=(
                require_aware(next_attempt_at, field="next_attempt_at")
                if next_attempt_at is not None
                else None
            ),
        )

    def cancel(self, *, now: datetime) -> Task:
        return self._transition(
            TaskStatus.CANCELLED, now=now, lease_owner=None, lease_expires_at=None
        )

    def reassign_for_retry(self, *, now: datetime) -> Task:
        """Return a failed Task to `assigned`, but only within the retry budget."""
        if self.status is not TaskStatus.FAILED:
            raise IllegalTransitionError("only a failed task can be retried", task_id=self.id)
        if not self.retry_budget_remains:
            raise RetryExhaustedError(
                f"task exhausted {self.max_attempts} attempts", task_id=self.id
            )
        return self._transition(
            TaskStatus.ASSIGNED, now=now, lease_owner=None, lease_expires_at=None
        )

    def convert_to_delegated(self, *, now: datetime) -> Task:
        """Interactive deadline reached: keep the same Task and let a Worker continue it.

        This must not create a second Task, and it must leave the Task claimable.
        """
        if self.execution_mode is not ExecutionMode.INTERACTIVE:
            raise IllegalTransitionError("task is not interactive", task_id=self.id)
        if self.status not in (TaskStatus.ASSIGNED, TaskStatus.CREATED):
            raise IllegalTransitionError(
                "interactive conversion requires an unstarted task", task_id=self.id
            )
        return self.model_copy(
            update={
                "status": TaskStatus.ASSIGNED,
                "execution_mode": ExecutionMode.DELEGATED,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": require_aware(now, field="now"),
                "row_version": self.row_version + 1,
            }
        )

    # -- predicates ------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self.status]

    @property
    def retry_budget_remains(self) -> bool:
        return self.attempt_count < self.max_attempts

    def lease_expired(self, *, now: datetime) -> bool:
        if self.status is not TaskStatus.RUNNING:
            return False
        if self.lease_expires_at is None:
            return True
        return require_aware(now, field="now") >= self.lease_expires_at

    def owns_lease(self, lease_owner: str, *, now: datetime) -> bool:
        return (
            self.status is TaskStatus.RUNNING
            and self.lease_owner == lease_owner
            and not self.lease_expired(now=now)
        )
