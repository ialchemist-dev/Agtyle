"""Delivery keys make duplicates impossible, and Approval binds one exact Action."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agtyle.domain.approvals import Approval, ApprovalInvalidReason, ApprovalStatus
from agtyle.domain.common import ErrorCode
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    reminder_due_delivery_key,
    task_terminal_delivery_key,
)
from agtyle.domain.tasks import TaskStatus

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
TASK_ID = "task_00000000-0000-7000-8000-000000000001"
REMINDER_ID = "rem_00000000-0000-7000-8000-000000000001"
ACTION_ID = "act_00000000-0000-7000-8000-000000000001"
PAYLOAD_HASH = "sha256:" + "a" * 64

DESTINATION = NotificationDestination(
    adapter="console", user_id="user_local", conversation_id="conv_demo"
)


def make_notification(**overrides: object) -> Notification:
    base: dict[str, object] = {
        "id": "not_00000000-0000-7000-8000-000000000001",
        "task_id": TASK_ID,
        "kind": NotificationKind.TASK_COMPLETED,
        "destination": DESTINATION,
        "payload": {"task_id": TASK_ID, "status": "completed"},
        "delivery_key": task_terminal_delivery_key(TASK_ID, TaskStatus.COMPLETED),
        "delivery_status": DeliveryStatus.PENDING,
        "max_attempts": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Notification(**base)  # type: ignore[arg-type]


def make_approval(**overrides: object) -> Approval:
    base: dict[str, object] = {
        "id": "apr_00000000-0000-7000-8000-000000000001",
        "action_request_id": ACTION_ID,
        "payload_hash": PAYLOAD_HASH,
        "principal_agent_id": "steward",
        "resource_type": "ReminderCollection",
        "resource_id": "user_local",
        "approved_by_user_id": "user_local",
        "status": ApprovalStatus.GRANTED,
        "expires_at": NOW + timedelta(minutes=10),
        "single_use": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Approval(**base)  # type: ignore[arg-type]


def _match_args(**overrides: object) -> dict[str, object]:
    args: dict[str, object] = {
        "action_request_id": ACTION_ID,
        "payload_hash": PAYLOAD_HASH,
        "principal_agent_id": "steward",
        "resource_type": "ReminderCollection",
        "resource_id": "user_local",
        "now": NOW,
    }
    args.update(overrides)
    return args


def test_reminder_delivery_key_is_stable() -> None:
    assert reminder_due_delivery_key(REMINDER_ID) == f"reminder_due:{REMINDER_ID}:once"
    assert reminder_due_delivery_key(REMINDER_ID) == reminder_due_delivery_key(REMINDER_ID)


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_task_terminal_delivery_key_is_stable(status: TaskStatus) -> None:
    key = task_terminal_delivery_key(TASK_ID, status)
    assert key == f"task_terminal:{TASK_ID}:{status.value}"
    assert key == task_terminal_delivery_key(TASK_ID, status)


@pytest.mark.parametrize("status", [TaskStatus.CREATED, TaskStatus.ASSIGNED, TaskStatus.RUNNING])
def test_delivery_key_refuses_a_non_terminal_status(status: TaskStatus) -> None:
    with pytest.raises(ValueError, match="terminal"):
        task_terminal_delivery_key(TASK_ID, status)


def test_notification_requires_a_task_or_a_reminder() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        make_notification(task_id=None, reminder_id=None)


def test_delivery_lifecycle_tracks_attempts_and_leases() -> None:
    pending = make_notification()
    delivering = pending.begin_delivery(owner="notifier-1", now=NOW, lease_seconds=30)
    assert delivering.delivery_status is DeliveryStatus.DELIVERING
    assert delivering.attempt_count == 1
    assert delivering.lease_expires_at == NOW + timedelta(seconds=30)

    delivered = delivering.mark_delivered(now=NOW + timedelta(seconds=1))
    assert delivered.delivery_status is DeliveryStatus.DELIVERED
    assert delivered.delivered_at == NOW + timedelta(seconds=1)
    assert delivered.lease_owner is None


def test_retry_returns_to_pending_with_a_future_attempt_time() -> None:
    delivering = make_notification().begin_delivery(owner="n1", now=NOW, lease_seconds=30)
    retried = delivering.schedule_retry(
        now=NOW,
        next_attempt_at=NOW + timedelta(seconds=2),
        error_code=ErrorCode.NOTIFICATION_DELIVERY_FAILED,
    )
    assert retried.delivery_status is DeliveryStatus.PENDING
    assert retried.next_attempt_at == NOW + timedelta(seconds=2)
    assert retried.attempts_remain


def test_exhausted_notification_becomes_terminally_failed() -> None:
    notification = make_notification(attempt_count=3, max_attempts=3)
    assert not notification.attempts_remain
    failed = notification.mark_failed(now=NOW, error_code=ErrorCode.NOTIFICATION_DELIVERY_FAILED)
    assert failed.delivery_status is DeliveryStatus.FAILED


def test_delivered_notification_cannot_be_redelivered() -> None:
    delivered = (
        make_notification()
        .begin_delivery(owner="n1", now=NOW, lease_seconds=30)
        .mark_delivered(now=NOW)
    )
    with pytest.raises(ValueError, match="delivered"):
        delivered.begin_delivery(owner="n2", now=NOW, lease_seconds=30)


def test_approval_matches_exact_action() -> None:
    approval = make_approval()
    assert approval.is_valid_for(**_match_args())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        (
            {"action_request_id": "act_00000000-0000-7000-8000-000000000009"},
            ApprovalInvalidReason.ACTION_MISMATCH,
        ),
        ({"payload_hash": "sha256:" + "b" * 64}, ApprovalInvalidReason.PAYLOAD_HASH_MISMATCH),
        ({"principal_agent_id": "research"}, ApprovalInvalidReason.PRINCIPAL_MISMATCH),
        ({"resource_id": "user_other"}, ApprovalInvalidReason.RESOURCE_MISMATCH),
        ({"resource_type": "Other"}, ApprovalInvalidReason.RESOURCE_MISMATCH),
    ],
)
def test_modified_or_mismatched_approval_does_not_authorize(
    override: dict[str, object], reason: ApprovalInvalidReason
) -> None:
    approval = make_approval()
    assert approval.invalid_reason(**_match_args(**override)) is reason  # type: ignore[arg-type]


def test_expired_or_consumed_approval_is_invalid() -> None:
    expired = make_approval(expires_at=NOW)
    assert expired.invalid_reason(**_match_args()) is ApprovalInvalidReason.EXPIRED  # type: ignore[arg-type]

    consumed = make_approval().consume(now=NOW)
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.invalid_reason(**_match_args()) is ApprovalInvalidReason.NOT_GRANTED  # type: ignore[arg-type]

    revoked = make_approval().revoke(now=NOW)
    assert revoked.invalid_reason(**_match_args()) is ApprovalInvalidReason.NOT_GRANTED  # type: ignore[arg-type]


def test_a_consumed_single_use_approval_cannot_be_consumed_again() -> None:
    consumed = make_approval().consume(now=NOW)
    with pytest.raises(ValueError, match="consumed"):
        consumed.consume(now=NOW)


def test_multi_use_approval_stays_granted_after_consumption() -> None:
    reusable = make_approval(single_use=False).consume(now=NOW)
    assert reusable.status is ApprovalStatus.GRANTED
    assert reusable.consumed_at == NOW
