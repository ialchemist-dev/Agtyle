"""Reminder scheduling: claim due Reminders and create exactly one due Notification each.

SQLite offers no `SKIP LOCKED`. Exclusion comes from a conditional update that moves a Reminder
from `scheduled` to `firing`; only the process that changes exactly one row proceeds to create
the Notification, so two Schedulers can never produce two due Notifications.
"""

from __future__ import annotations

from datetime import datetime

from agtyle.domain.common import DomainModel, IdPrefix
from agtyle.domain.events import Event, EventType, system_actor
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    reminder_due_delivery_key,
)
from agtyle.domain.reminders import Reminder
from agtyle.observability.logging import get_logger
from agtyle.ports.clock import ClockPort
from agtyle.ports.failure_injection import (
    Checkpoint,
    FailureInjectorPort,
    NullFailureInjector,
)
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.repositories import UnitOfWorkFactory

logger = get_logger(__name__)


class FiredReminder(DomainModel):
    reminder_id: str
    notification_id: str
    delivery_key: str


class ReminderService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: ClockPort,
        ids: IdGeneratorPort,
        lease_seconds: int,
        notification_adapter: str,
        max_notification_attempts: int,
        failures: FailureInjectorPort | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._lease_seconds = lease_seconds
        self._notification_adapter = notification_adapter
        self._max_notification_attempts = max_notification_attempts
        self._failures = failures or NullFailureInjector()

    async def claim_due(self) -> FiredReminder | None:
        """Atomic operation 6: claim, transition to firing, create the due Notification."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            reminder = await uow.reminders.claim_next_due(
                owner=self._ids.new_id(IdPrefix.EVENT),
                now=now,
                lease_seconds=self._lease_seconds,
            )
            if reminder is None:
                await uow.rollback()
                return None

            delivery_key = reminder_due_delivery_key(reminder.id)
            existing = await uow.notifications.get_by_delivery_key(delivery_key)
            if existing is not None:
                # A crash after a previous commit already produced the Notification.
                await uow.rollback()
                return FiredReminder(
                    reminder_id=reminder.id,
                    notification_id=existing.id,
                    delivery_key=delivery_key,
                )

            source_task = await uow.tasks.get(reminder.source_task_id)
            conversation_id = (
                source_task.origin.conversation_id
                if source_task is not None
                else f"reminder:{reminder.id}"
            )
            notification = self._due_notification(
                reminder, delivery_key=delivery_key, conversation_id=conversation_id, now=now
            )
            await uow.notifications.add(notification)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.REMINDER_FIRING,
                    subject=reminder.id,
                    actor=system_actor(),
                    time=now,
                    data={
                        "task_id": reminder.source_task_id,
                        "notification_id": notification.id,
                        "delivery_key": delivery_key,
                        "scheduled_for_utc": reminder.scheduled_for_utc.isoformat(),
                    },
                )
            )
            await uow.commit()

        await self._failures.checkpoint(Checkpoint.AFTER_REMINDER_FIRING_COMMIT)
        return FiredReminder(
            reminder_id=reminder.id,
            notification_id=notification.id,
            delivery_key=delivery_key,
        )

    def _due_notification(
        self, reminder: Reminder, *, delivery_key: str, conversation_id: str, now: datetime
    ) -> Notification:
        moment = now
        return Notification(
            id=self._ids.new_id(IdPrefix.NOTIFICATION),
            reminder_id=reminder.id,
            task_id=reminder.source_task_id,
            kind=NotificationKind.REMINDER_DUE,
            destination=NotificationDestination(
                adapter=self._notification_adapter,
                user_id=reminder.user_id,
                conversation_id=conversation_id,
            ),
            payload={
                "reminder_id": reminder.id,
                "task_id": reminder.source_task_id,
                "title": reminder.title,
                "note": reminder.note,
                "scheduled_for_utc": reminder.scheduled_for_utc.isoformat(),
                "timezone": reminder.timezone,
                "summary_code": "reminder_due",
            },
            delivery_key=delivery_key,
            delivery_status=DeliveryStatus.PENDING,
            max_attempts=self._max_notification_attempts,
            created_at=moment,
            updated_at=moment,
        )
