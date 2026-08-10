"""Notification delivery: claim, deliver, and record the outcome durably.

Delivery is at-least-once at the adapter boundary. The Notification record itself is created
exactly once through its unique delivery key, and the local recording adapter deduplicates, so
acceptance can observe exactly-once delivery end to end.
"""

from __future__ import annotations

from enum import StrEnum

from agtyle.application.retry_policy import RetryPolicy
from agtyle.domain.common import (
    DomainModel,
    ErrorCode,
    IdPrefix,
    redact_secrets,
)
from agtyle.domain.events import Event, EventType, system_actor
from agtyle.domain.notifications import Notification, NotificationKind
from agtyle.domain.reminders import ReminderStatus
from agtyle.observability.logging import LogContext, get_logger
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.notification import NotificationPort
from agtyle.ports.repositories import UnitOfWorkFactory

logger = get_logger(__name__)


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    NOTHING_TO_DO = "nothing_to_do"


class DeliveryReport(DomainModel):
    outcome: DeliveryOutcome
    notification_id: str | None = None
    delivery_key: str | None = None
    reminder_id: str | None = None
    duplicate_suppressed: bool = False


class NotificationService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        adapters: dict[str, NotificationPort],
        clock: ClockPort,
        ids: IdGeneratorPort,
        retry_policy: RetryPolicy,
        lease_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._adapters = adapters
        self._clock = clock
        self._ids = ids
        self._retry = retry_policy
        self._lease_seconds = lease_seconds

    async def claim_pending(self, *, owner: str) -> Notification | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            claimed = await uow.notifications.claim_next_pending(
                owner=owner, now=now, lease_seconds=self._lease_seconds
            )
            if claimed is None:
                await uow.rollback()
                return None
            await uow.commit()
        return claimed

    async def deliver_claimed(self, notification: Notification, *, owner: str) -> DeliveryReport:
        """Deliver one claimed Notification and record the outcome in a single transaction."""
        with LogContext(notification_id=notification.id, worker_id=owner):
            adapter = self._adapters.get(notification.destination.adapter)
            if adapter is None:
                return await self._record_failure(
                    notification,
                    detail=f"no adapter named {notification.destination.adapter!r}",
                    permanent=True,
                )
            try:
                result = await adapter.deliver(notification)
            except Exception as exc:
                return await self._record_failure(notification, detail=str(exc), permanent=False)

            if not result.delivered:
                return await self._record_failure(
                    notification,
                    detail=result.detail or "adapter reported failure",
                    permanent=False,
                )
            return await self._record_delivery(
                notification, duplicate_suppressed=result.duplicate_suppressed
            )

    async def _record_delivery(
        self, notification: Notification, *, duplicate_suppressed: bool
    ) -> DeliveryReport:
        """Marking the Notification delivered and its Reminder delivered is one transaction."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            current = await uow.notifications.get(notification.id)
            if current is None:  # pragma: no cover - the row was just claimed
                await uow.rollback()
                return DeliveryReport(outcome=DeliveryOutcome.NOTHING_TO_DO)

            delivered = current.mark_delivered(now=now)
            await uow.notifications.update(delivered, expected_row_version=current.row_version)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.NOTIFICATION_DELIVERED,
                    subject=current.task_id or current.reminder_id or current.id,
                    actor=system_actor(),
                    time=now,
                    data={
                        "notification_id": current.id,
                        "delivery_key": current.delivery_key,
                        "kind": current.kind.value,
                        "duplicate_suppressed": duplicate_suppressed,
                    },
                )
            )

            reminder_id: str | None = None
            if current.kind is NotificationKind.REMINDER_DUE and current.reminder_id:
                reminder = await uow.reminders.get(current.reminder_id)
                if reminder is not None and reminder.status is not ReminderStatus.DELIVERED:
                    await uow.reminders.update(
                        reminder.mark_delivered(now=now),
                        expected_row_version=reminder.row_version,
                    )
                    await uow.events.append(
                        Event(
                            id=self._ids.new_id(IdPrefix.EVENT),
                            type=EventType.REMINDER_DELIVERED,
                            subject=reminder.id,
                            actor=system_actor(),
                            time=now,
                            data={
                                "notification_id": current.id,
                                "task_id": reminder.source_task_id,
                            },
                        )
                    )
                reminder_id = current.reminder_id

            await uow.commit()

        return DeliveryReport(
            outcome=DeliveryOutcome.DELIVERED,
            notification_id=notification.id,
            delivery_key=notification.delivery_key,
            reminder_id=reminder_id,
            duplicate_suppressed=duplicate_suppressed,
        )

    async def _record_failure(
        self, notification: Notification, *, detail: str, permanent: bool
    ) -> DeliveryReport:
        now = self._clock.now()
        safe_detail = redact_secrets(detail)[:300]
        async with self._uow_factory() as uow:
            current = await uow.notifications.get(notification.id)
            if current is None:  # pragma: no cover
                await uow.rollback()
                return DeliveryReport(outcome=DeliveryOutcome.NOTHING_TO_DO)

            retryable = not permanent and current.attempts_remain
            if retryable:
                updated = current.schedule_retry(
                    now=now,
                    next_attempt_at=self._retry.next_attempt_at(
                        now=now, attempt_count=current.attempt_count, seed=current.delivery_key
                    ),
                    error_code=ErrorCode.NOTIFICATION_DELIVERY_FAILED,
                )
                await uow.notifications.update(updated, expected_row_version=current.row_version)
                await uow.commit()
                logger.warning("notification delivery will retry", extra={"detail": safe_detail})
                return DeliveryReport(
                    outcome=DeliveryOutcome.RETRY_SCHEDULED,
                    notification_id=current.id,
                    delivery_key=current.delivery_key,
                )

            failed = current.mark_failed(now=now, error_code=ErrorCode.NOTIFICATION_DELIVERY_FAILED)
            await uow.notifications.update(failed, expected_row_version=current.row_version)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.NOTIFICATION_FAILED,
                    subject=current.task_id or current.reminder_id or current.id,
                    actor=system_actor(),
                    time=now,
                    data={
                        "notification_id": current.id,
                        "delivery_key": current.delivery_key,
                        "detail": safe_detail,
                    },
                )
            )
            # A due Notification that can never be delivered leaves its Reminder failed, and the
            # baseline deliberately creates no recursive failure notification.
            if current.kind is NotificationKind.REMINDER_DUE and current.reminder_id:
                reminder = await uow.reminders.get(current.reminder_id)
                if reminder is not None and not reminder.is_terminal:
                    await uow.reminders.update(
                        reminder.mark_failed(now=now),
                        expected_row_version=reminder.row_version,
                    )
                    await uow.events.append(
                        Event(
                            id=self._ids.new_id(IdPrefix.EVENT),
                            type=EventType.REMINDER_FAILED,
                            subject=reminder.id,
                            actor=system_actor(),
                            time=now,
                            data={"notification_id": current.id},
                        )
                    )
            await uow.commit()

        return DeliveryReport(
            outcome=DeliveryOutcome.FAILED,
            notification_id=notification.id,
            delivery_key=notification.delivery_key,
        )
