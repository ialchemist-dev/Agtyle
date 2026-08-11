"""Notification: the durable fact that closes the loop back to the user."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from agtyle.domain.common import (
    DomainModel,
    ErrorCode,
    JsonMapping,
    NotificationId,
    ReminderId,
    TaskId,
    UserId,
    UtcDatetime,
    require_aware,
)
from agtyle.domain.tasks import TaskStatus


class NotificationKind(StrEnum):
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    APPROVAL_REQUIRED = "approval_required"
    REMINDER_DUE = "reminder_due"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"


def reminder_due_delivery_key(reminder_id: str) -> str:
    """Stable key that makes at most one due Notification possible per Reminder."""
    return f"reminder_due:{reminder_id}:once"


def task_terminal_delivery_key(task_id: str, status: TaskStatus) -> str:
    """Stable key that makes at most one terminal Notification possible per Task outcome."""
    if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise ValueError(f"{status.value} is not a terminal task status")
    return f"task_terminal:{task_id}:{status.value}"


class NotificationDestination(DomainModel):
    """Structured routing. Rendering belongs to the adapter, not to the record."""

    adapter: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    user_id: UserId
    conversation_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Notification(DomainModel):
    """One delivery obligation with its own lease, retry budget and unique delivery key."""

    id: NotificationId
    task_id: TaskId | None = None
    reminder_id: ReminderId | None = None
    kind: NotificationKind
    destination: NotificationDestination
    payload: JsonMapping = Field(default_factory=dict)
    delivery_key: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    delivery_status: DeliveryStatus
    attempt_count: Annotated[int, Field(ge=0)] = 0
    max_attempts: Annotated[int, Field(ge=1)]
    lease_owner: str | None = None
    lease_expires_at: UtcDatetime | None = None
    next_attempt_at: UtcDatetime | None = None
    last_error_code: ErrorCode | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    delivered_at: UtcDatetime | None = None
    row_version: Annotated[int, Field(ge=1)] = 1

    def model_post_init(self, __context: object) -> None:
        if self.task_id is None and self.reminder_id is None:
            raise ValueError("a notification must reference a task or a reminder")

    def begin_delivery(self, *, owner: str, now: datetime, lease_seconds: int) -> Notification:
        if self.delivery_status not in (DeliveryStatus.PENDING, DeliveryStatus.DELIVERING):
            raise ValueError(
                f"cannot deliver a notification in status {self.delivery_status.value}"
            )
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "delivery_status": DeliveryStatus.DELIVERING,
                "lease_owner": owner,
                "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                "attempt_count": self.attempt_count + 1,
                "updated_at": moment,
                "next_attempt_at": None,
                "row_version": self.row_version + 1,
            }
        )

    def mark_delivered(self, *, now: datetime) -> Notification:
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "delivery_status": DeliveryStatus.DELIVERED,
                "delivered_at": moment,
                "updated_at": moment,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "row_version": self.row_version + 1,
            }
        )

    def schedule_retry(
        self, *, now: datetime, next_attempt_at: datetime, error_code: ErrorCode
    ) -> Notification:
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "delivery_status": DeliveryStatus.PENDING,
                "next_attempt_at": require_aware(next_attempt_at, field="next_attempt_at"),
                "last_error_code": error_code,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )

    def mark_failed(self, *, now: datetime, error_code: ErrorCode) -> Notification:
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "delivery_status": DeliveryStatus.FAILED,
                "last_error_code": error_code,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )

    @property
    def attempts_remain(self) -> bool:
        return self.attempt_count < self.max_attempts

    def lease_expired(self, *, now: datetime) -> bool:
        if self.delivery_status is not DeliveryStatus.DELIVERING:
            return False
        if self.lease_expires_at is None:
            return True
        return require_aware(now, field="now") >= self.lease_expires_at
