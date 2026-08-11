"""The reminder vertical slice, end to end, through the real Cedar engine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.application.execution_service import ExecutionOutcome
from agtyle.application.interaction_service import InteractionOutcome
from agtyle.bootstrap import Container
from agtyle.domain.actions import ActionStatus, CedarDecision, PolicyOutcome
from agtyle.domain.agents import AgentRunStatus
from agtyle.domain.events import EventType
from agtyle.domain.notifications import (
    DeliveryStatus,
    NotificationKind,
    reminder_due_delivery_key,
    task_terminal_delivery_key,
)
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import ExecutionMode, TaskStatus
from agtyle.ports.clock import FrozenClock
from agtyle.workers.notification_worker import NotificationWorker
from agtyle.workers.scheduler import Scheduler
from agtyle.workers.task_worker import TaskWorker

from .conftest import DUE, interaction, record_counts


async def test_delegated_reminder_returns_persisted_receipt(container: Container) -> None:
    result = await container.interaction_service.handle(interaction())

    assert result.outcome is InteractionOutcome.TASK_ASSIGNED
    receipt = result.task_receipt
    assert receipt is not None
    assert receipt.status is TaskStatus.ASSIGNED
    assert receipt.assigned_agent_id == "steward"

    # The receipt is only meaningful if the state it describes is already committed.
    async with container.uow_factory() as uow:
        task = await uow.tasks.get(receipt.task_id)
        intent = await uow.intents.get(result.intent_id)
        events = await uow.events.list_by_subject(receipt.task_id)
    assert task is not None
    assert task.status is TaskStatus.ASSIGNED
    assert intent is not None
    assert intent.original_input == interaction().input
    assert [event.type for event in events] == [EventType.TASK_ASSIGNED]


async def test_worker_creates_reminder_through_real_cedar(
    container: Container, task_worker: TaskWorker
) -> None:
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None

    report = await task_worker.run_once()
    assert report is not None
    assert report.outcome is ExecutionOutcome.COMPLETED
    assert report.policy_outcome is PolicyOutcome.ALLOW

    async with container.uow_factory() as uow:
        action = await uow.actions.get(report.action_request_id or "")
        decisions = await uow.actions.list_policy_decisions(action.id if action else "")
        reminder = await uow.reminders.get(report.reminder_id or "")

    assert action is not None
    assert action.status is ActionStatus.APPLIED
    assert action.hash_matches()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.decision is PolicyOutcome.ALLOW
    assert decision.cedar_decision is CedarDecision.ALLOW
    assert decision.determining_policy_ids == ["permit-steward-direct-reminder"]
    assert decision.cedar_version == "4.12.0"
    assert reminder is not None
    assert reminder.status is ReminderStatus.SCHEDULED
    assert reminder.scheduled_for_utc == DUE
    assert reminder.timezone == "America/Denver"


async def test_success_produces_the_required_record_cardinality(
    container: Container, task_worker: TaskWorker
) -> None:
    await container.interaction_service.handle(interaction())
    await task_worker.run_once()

    counts = await record_counts(container)
    assert counts["intents"] == 1
    assert counts["tasks"] == 1
    assert counts["agent_runs"] == 1
    assert counts["action_requests"] == 1
    assert counts["policy_decisions"] == 1
    assert counts["action_results"] == 1
    assert counts["reminders"] == 1
    assert counts["notifications"] == 1
    assert counts["approvals"] == 0


async def test_completion_is_atomic_with_event_and_notification(
    container: Container, task_worker: TaskWorker
) -> None:
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    report = await task_worker.run_once()
    assert report is not None

    async with container.uow_factory() as uow:
        task = await uow.tasks.get(report.task_id)
        runs = await uow.agent_runs.list_for_task(report.task_id)
        notifications = await uow.notifications.list_for_task(report.task_id)
        events = await uow.events.list_by_subject(report.task_id)

    assert task is not None
    assert task.status is TaskStatus.COMPLETED
    assert [run.status for run in runs] == [AgentRunStatus.SUCCEEDED]
    assert len(notifications) == 1
    assert notifications[0].delivery_key == task_terminal_delivery_key(
        report.task_id, TaskStatus.COMPLETED
    )
    assert notifications[0].delivery_status is DeliveryStatus.PENDING

    types = [event.type for event in events]
    for expected in (
        EventType.TASK_ASSIGNED,
        EventType.TASK_STARTED,
        EventType.ACTION_AUTHORIZED,
        EventType.REMINDER_CREATED,
        EventType.TASK_COMPLETED,
    ):
        assert expected in types, f"missing {expected.value}"


async def test_scheduler_creates_one_due_notification(
    container: Container,
    task_worker: TaskWorker,
    scheduler: Scheduler,
    notification_worker: NotificationWorker,
    clock: FrozenClock,
) -> None:
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    await notification_worker.run_once()

    # One microsecond before the due time nothing may fire.
    clock.set(DUE - timedelta(microseconds=1))
    assert await scheduler.run_once() is None

    clock.set(DUE)
    fired = await scheduler.run_once()
    assert fired is not None
    assert fired.delivery_key == reminder_due_delivery_key(report.reminder_id or "")

    # Running again must not create a second due Notification.
    assert await scheduler.run_once() is None

    async with container.uow_factory() as uow:
        notifications = await uow.notifications.list_for_reminder(report.reminder_id or "")
        reminder = await uow.reminders.get(report.reminder_id or "")
    assert len(notifications) == 1
    assert notifications[0].kind is NotificationKind.REMINDER_DUE
    assert reminder is not None
    assert reminder.status is ReminderStatus.FIRING


async def test_notification_delivery_marks_reminder_delivered(
    container: Container,
    task_worker: TaskWorker,
    scheduler: Scheduler,
    notification_worker: NotificationWorker,
    recorder: RecordingNotificationAdapter,
    clock: FrozenClock,
) -> None:
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None

    completion = await notification_worker.run_once()
    assert completion is not None
    assert completion.outcome.value == "delivered"

    clock.set(DUE)
    await scheduler.run_once()
    due = await notification_worker.run_once()
    assert due is not None
    assert due.reminder_id == report.reminder_id

    async with container.uow_factory() as uow:
        reminder = await uow.reminders.get(report.reminder_id or "")
        notifications = await uow.notifications.list_for_task(report.task_id)
        events = await uow.events.list_by_subject(report.reminder_id or "")

    assert reminder is not None
    assert reminder.status is ReminderStatus.DELIVERED
    assert reminder.delivered_at == clock.now()
    assert all(item.delivery_status is DeliveryStatus.DELIVERED for item in notifications)
    assert {event.type for event in events} >= {
        EventType.REMINDER_FIRING,
        EventType.REMINDER_DELIVERED,
    }
    assert sorted(recorder.delivered_keys) == sorted(
        [
            task_terminal_delivery_key(report.task_id, TaskStatus.COMPLETED),
            reminder_due_delivery_key(report.reminder_id or ""),
        ]
    )


async def test_timeline_contains_complete_explanation(
    container: Container,
    task_worker: TaskWorker,
    scheduler: Scheduler,
    notification_worker: NotificationWorker,
    clock: FrozenClock,
) -> None:
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    await notification_worker.run_once()
    clock.set(DUE)
    await scheduler.run_once()
    await notification_worker.run_once()

    timeline = await container.timeline_service.for_task(report.task_id)
    assert timeline is not None
    assert timeline.current_state["status"] == TaskStatus.COMPLETED.value

    kinds = {entry.kind for entry in timeline.history}
    assert kinds >= {
        "agent_run",
        "action_request",
        "policy_decision",
        "action_result",
        "reminder",
        "notification",
        "event",
    }
    assert timeline.record_counts["policy_decisions"] == 1
    assert timeline.record_counts["reminders"] == 1
    assert timeline.record_counts["notifications"] == 2

    # Every record links back to the originating Task.
    references = {entry.reference for entry in timeline.history}
    assert report.action_request_id in references
    assert report.reminder_id in references
    assert timeline.history == sorted(
        timeline.history, key=lambda entry: (entry.at, entry.kind, entry.reference)
    )


async def test_same_interaction_key_returns_same_receipt(
    container: Container, task_worker: TaskWorker
) -> None:
    first = await container.interaction_service.handle(interaction())
    await task_worker.run_once()
    before = await record_counts(container)

    second = await container.interaction_service.handle(interaction())
    after = await record_counts(container)

    assert second.replayed
    assert second.task_receipt is not None
    assert first.task_receipt is not None
    assert second.task_receipt.task_id == first.task_receipt.task_id
    assert after == before


async def test_reused_key_with_a_different_body_is_a_conflict(container: Container) -> None:
    from agtyle.domain.common import InteractionIdempotencyConflictError

    await container.interaction_service.handle(interaction())
    before = await record_counts(container)

    with pytest.raises(InteractionIdempotencyConflictError):
        await container.interaction_service.handle(
            interaction(text="Remind me to do something else at 2026-08-09T22:05:00Z")
        )
    assert await record_counts(container) == before


async def test_restart_creates_no_duplicate_effect(
    container: Container,
    task_worker: TaskWorker,
    scheduler: Scheduler,
    notification_worker: NotificationWorker,
    clock: FrozenClock,
) -> None:
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    await notification_worker.run_once()
    clock.set(DUE)
    await scheduler.run_once()
    await notification_worker.run_once()

    before = await record_counts(container)

    # "Restart" every role against the same database and drain again.
    for _ in range(3):
        assert await task_worker.run_once() is None
        assert await scheduler.run_once() is None
        assert await notification_worker.run_once() is None

    assert await record_counts(container) == before


async def test_clarification_creates_an_intent_but_no_task(container: Container) -> None:
    result = await container.interaction_service.handle(
        interaction(text="Remind me to call mum tomorrow")
    )
    assert result.outcome is InteractionOutcome.CLARIFICATION_REQUIRED
    assert result.task_receipt is None
    counts = await record_counts(container)
    assert counts["intents"] == 1
    assert counts["tasks"] == 0


async def test_recurring_request_is_rejected_as_unsupported(container: Container) -> None:
    result = await container.interaction_service.handle(
        interaction(text="Remind me to stretch every day at 2026-08-09T22:05:00Z")
    )
    assert result.outcome is InteractionOutcome.CLARIFICATION_REQUIRED
    assert "Recurring" in result.message


async def test_interactive_mode_completes_within_budget(
    container: Container, clock: FrozenClock
) -> None:
    result = await container.interaction_service.handle(interaction(mode=ExecutionMode.INTERACTIVE))
    assert result.outcome is InteractionOutcome.COMPLETED
    assert result.result is not None
    reminder_id = result.result["reminder_id"]

    async with container.uow_factory() as uow:
        reminder = await uow.reminders.get(reminder_id)
        task = await uow.tasks.get(result.task_receipt.task_id if result.task_receipt else "")
    assert reminder is not None
    assert task is not None
    assert task.status is TaskStatus.COMPLETED

    # The scheduled due notification is still required even in interactive mode.
    clock.set(DUE)
    fired = await container.reminder_service.claim_due()
    assert fired is not None
