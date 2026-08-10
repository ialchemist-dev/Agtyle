"""Recovery: reclaim work abandoned by a crashed process.

A Worker that dies mid-attempt leaves a Task `running` with an expired lease and an AgentRun
still `running`. Recovery abandons that run and either returns the Task to `assigned` within its
retry budget or fails it terminally. It never deletes or mutates audit history.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agtyle.application.retry_policy import RetryPolicy
from agtyle.domain.agents import AgentRunStatus
from agtyle.domain.common import ErrorCode, IdPrefix
from agtyle.domain.events import Event, EventType, system_actor
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    task_terminal_delivery_key,
)
from agtyle.domain.tasks import Task, TaskStatus
from agtyle.observability.logging import get_logger
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.repositories import UnitOfWorkFactory, UnitOfWorkPort

logger = get_logger(__name__)


class RecoverySummary(BaseModel):
    """Mutable on purpose: the service accumulates outcomes as it walks the stale rows."""

    model_config = ConfigDict(extra="forbid")

    reassigned_task_ids: list[str] = Field(default_factory=list)
    failed_task_ids: list[str] = Field(default_factory=list)
    released_notification_ids: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.reassigned_task_ids)
            + len(self.failed_task_ids)
            + len(self.released_notification_ids)
        )


class RecoveryService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: ClockPort,
        ids: IdGeneratorPort,
        retry_policy: RetryPolicy,
        notification_adapter: str,
        max_notification_attempts: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._retry = retry_policy
        self._notification_adapter = notification_adapter
        self._max_notification_attempts = max_notification_attempts

    async def recover_expired_leases(self) -> RecoverySummary:
        """Atomic operation 8, applied per Task: abandon the old run, then reassign or fail."""
        now = self._clock.now()
        summary = RecoverySummary()

        async with self._uow_factory() as uow:
            stale_tasks = await uow.tasks.list_expired_running(now=now)
            for task in stale_tasks:
                await self._recover_task(uow, task, summary)

            stale_notifications = await uow.notifications.list_expired_delivering(now=now)
            for notification in stale_notifications:
                released = notification.schedule_retry(
                    now=now,
                    next_attempt_at=now,
                    error_code=ErrorCode.NOTIFICATION_DELIVERY_FAILED,
                )
                await uow.notifications.update(
                    released, expected_row_version=notification.row_version
                )
                summary.released_notification_ids.append(notification.id)

            await uow.commit()

        if summary.total:
            logger.info(
                "recovered abandoned work",
                extra={
                    "reassigned": len(summary.reassigned_task_ids),
                    "failed": len(summary.failed_task_ids),
                    "notifications_released": len(summary.released_notification_ids),
                },
            )
        return summary

    async def _recover_task(
        self, uow: UnitOfWorkPort, task: Task, summary: RecoverySummary
    ) -> None:
        now = self._clock.now()

        for run in await uow.agent_runs.list_for_task(task.id):
            if run.status is AgentRunStatus.RUNNING:
                await uow.agent_runs.update(run.abandon(now=now))

        failed = task.fail(
            now=now,
            error_code=ErrorCode.LEASE_LOST,
            error_message="the worker holding this task stopped renewing its lease",
        )
        await uow.events.append(
            Event(
                id=self._ids.new_id(IdPrefix.EVENT),
                type=EventType.TASK_RECOVERED,
                subject=task.id,
                actor=system_actor(),
                time=now,
                data={
                    "previous_lease_owner": task.lease_owner,
                    "attempt_count": task.attempt_count,
                },
            )
        )

        if failed.retry_budget_remains:
            reassigned = failed.reassign_for_retry(now=now)
            await uow.tasks.update(reassigned, expected_row_version=task.row_version)
            summary.reassigned_task_ids.append(task.id)
            return

        await uow.tasks.update(failed, expected_row_version=task.row_version)
        await uow.events.append(
            Event(
                id=self._ids.new_id(IdPrefix.EVENT),
                type=EventType.TASK_FAILED,
                subject=task.id,
                actor=system_actor(),
                time=now,
                data={"error_code": ErrorCode.RETRY_EXHAUSTED.value},
            )
        )
        delivery_key = task_terminal_delivery_key(task.id, TaskStatus.FAILED)
        if await uow.notifications.get_by_delivery_key(delivery_key) is None:
            await uow.notifications.add(
                Notification(
                    id=self._ids.new_id(IdPrefix.NOTIFICATION),
                    task_id=task.id,
                    kind=NotificationKind.TASK_FAILED,
                    destination=NotificationDestination(
                        adapter=self._notification_adapter,
                        user_id=task.origin.user_id,
                        conversation_id=task.origin.conversation_id,
                    ),
                    payload={
                        "task_id": task.id,
                        "status": TaskStatus.FAILED.value,
                        "error_code": ErrorCode.RETRY_EXHAUSTED.value,
                        "summary_code": "task_failed",
                    },
                    delivery_key=delivery_key,
                    delivery_status=DeliveryStatus.PENDING,
                    max_attempts=self._max_notification_attempts,
                    created_at=now,
                    updated_at=now,
                )
            )
        summary.failed_task_ids.append(task.id)
