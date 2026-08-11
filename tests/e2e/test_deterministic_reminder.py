"""The deterministic end-to-end scenario from the specification, §21.

It runs in CI with no wall-clock waiting: the clock is injected and advanced explicitly. It
writes a redacted JSON proof artifact so a reviewer can check the exact record cardinality
without re-running anything.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.application.execution_service import ExecutionOutcome
from agtyle.application.interaction_service import InteractionOutcome
from agtyle.bootstrap import Container, build_container, migrate
from agtyle.config import CEDAR_PINNED_VERSION, Settings
from agtyle.domain.actions import CedarDecision, PolicyOutcome
from agtyle.domain.events import EventType
from agtyle.domain.notifications import (
    DeliveryStatus,
    reminder_due_delivery_key,
    task_terminal_delivery_key,
)
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import TaskStatus
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator
from agtyle.workers.notification_worker import NotificationWorker
from agtyle.workers.scheduler import Scheduler
from agtyle.workers.task_worker import TaskWorker
from tests.integration.conftest import DUE, interaction, record_counts

REPO_ROOT = Path(__file__).resolve().parents[2]
PROOF_PATH = REPO_ROOT / "artifacts" / "verification" / "deterministic-reminder-e2e.json"

REQUIRED_EVENT_TYPES = (
    EventType.INTENT_ACCEPTED,
    EventType.TASK_ASSIGNED,
    EventType.TASK_STARTED,
    EventType.ACTION_AUTHORIZED,
    EventType.REMINDER_CREATED,
    EventType.TASK_COMPLETED,
    EventType.NOTIFICATION_DELIVERED,
    EventType.REMINDER_FIRING,
    EventType.REMINDER_DELIVERED,
)


def _run_concurrently(factories: list[Any]) -> list[Any]:
    """Run coroutine factories on separate threads and event loops, as separate processes would."""
    with ThreadPoolExecutor(max_workers=len(factories)) as pool:
        return list(pool.map(lambda factory: asyncio.run(factory()), factories))


@pytest.fixture
def e2e(settings: Settings, clock: FrozenClock) -> Any:
    """One database, one clock, and containers that can be rebuilt to simulate a restart."""
    migrate(settings)
    recorder = RecordingNotificationAdapter()
    built: list[Container] = []

    def restart() -> Container:
        container = build_container(
            settings,
            clock=clock,
            ids=DeterministicIdGenerator(seed=len(built)),
            notification_adapters={recorder.adapter_name: recorder},
            configure_logs=False,
        )
        built.append(container)
        return container

    try:
        yield restart, recorder, clock
    finally:
        for container in built:
            container.dispose()


async def test_deterministic_reminder_end_to_end(e2e: Any) -> None:
    restart, recorder, clock = e2e
    container: Container = restart()

    def task_worker(owner: str, target: Container | None = None) -> TaskWorker:
        source = target or container
        return TaskWorker(
            execution=source.execution_service,
            recovery=source.recovery_service,
            poll_interval_seconds=0.001,
            lease_seconds=source.settings.task_lease_seconds,
            owner=owner,
        )

    def scheduler(target: Container) -> Scheduler:
        return Scheduler(reminders=target.reminder_service, poll_interval_seconds=0.001)

    def notifier(owner: str, target: Container) -> NotificationWorker:
        return NotificationWorker(
            notifications=target.notification_service, poll_interval_seconds=0.001, owner=owner
        )

    # 1-2. Submit the interaction and assert the receipt describes committed state.
    result = await container.interaction_service.handle(interaction(key="e2e-reminder-001"))
    assert result.outcome is InteractionOutcome.TASK_ASSIGNED
    assert result.task_receipt is not None
    task_id = result.task_receipt.task_id
    assert result.task_receipt.status is TaskStatus.ASSIGNED

    async with container.uow_factory() as uow:
        assert (await uow.tasks.get(task_id)) is not None

    # 3-4. One Worker pass completes the Task through the real Cedar engine.
    report = await task_worker("worker-1").run_once()
    assert report is not None
    assert report.outcome is ExecutionOutcome.COMPLETED
    assert report.policy_outcome is PolicyOutcome.ALLOW
    reminder_id = report.reminder_id
    assert reminder_id is not None

    counts = await record_counts(container)
    assert counts["policy_decisions"] == 1
    assert counts["action_results"] == 1
    assert counts["reminders"] == 1
    assert counts["notifications"] == 1

    async with container.uow_factory() as uow:
        action = await uow.actions.get(report.action_request_id or "")
        decisions = await uow.actions.list_policy_decisions(action.id if action else "")
    decision = decisions[0]
    assert decision.cedar_decision is CedarDecision.ALLOW
    assert decision.cedar_version == CEDAR_PINNED_VERSION

    # 5-6. Deliver the completion Notification.
    completion = await notifier("notifier-1", container).run_once()
    assert completion is not None
    assert completion.outcome.value == "delivered"

    # 7-8. One microsecond before the due time, nothing is due.
    clock.set(DUE - timedelta(microseconds=1))
    assert await scheduler(container).run_once() is None

    # 9-10. At the exact due time, two concurrent Schedulers must produce one Notification.
    clock.set(DUE)
    scheduler_a, scheduler_b = restart(), restart()
    fired = _run_concurrently([scheduler(scheduler_a).run_once, scheduler(scheduler_b).run_once])
    assert len([item for item in fired if item is not None]) == 1

    async with container.uow_factory() as uow:
        due_notifications = await uow.notifications.list_for_reminder(reminder_id)
    assert len(due_notifications) == 1

    # 11. Two concurrent Notification Workers must produce one observed delivery.
    notifier_a, notifier_b = restart(), restart()
    delivered = _run_concurrently(
        [
            notifier("notifier-a", notifier_a).run_once,
            notifier("notifier-b", notifier_b).run_once,
        ]
    )
    assert len([item for item in delivered if item is not None]) == 1
    assert sorted(recorder.delivered_keys) == sorted(
        [
            task_terminal_delivery_key(task_id, TaskStatus.COMPLETED),
            reminder_due_delivery_key(reminder_id),
        ]
    )

    before_restart = await record_counts(container)

    # 12-13. Restart every role against the same database and drain again.
    restarted = restart()
    for _ in range(2):
        assert await task_worker("worker-restarted", restarted).run_once() is None
        assert await scheduler(restarted).run_once() is None
        assert await notifier("notifier-restarted", restarted).run_once() is None
    assert await record_counts(restarted) == before_restart

    # 14. A duplicate interaction returns the same receipt and changes nothing.
    replay = await restarted.interaction_service.handle(interaction(key="e2e-reminder-001"))
    assert replay.replayed
    assert replay.task_receipt is not None
    assert replay.task_receipt.task_id == task_id
    assert await record_counts(restarted) == before_restart

    # 15. The timeline links every required identifier back to the original Task.
    timeline = await restarted.timeline_service.for_task(task_id)
    assert timeline is not None
    references = {entry.reference for entry in timeline.history}
    assert {report.action_request_id, reminder_id, report.agent_run_id} <= references

    async with restarted.uow_factory() as uow:
        reminder = await uow.reminders.get(reminder_id)
        notifications = await uow.notifications.list_for_task(task_id)
        events = await uow.events.list_by_subjects([task_id, result.intent_id, reminder_id])
    assert reminder is not None
    assert reminder.status is ReminderStatus.DELIVERED
    assert all(item.delivery_status is DeliveryStatus.DELIVERED for item in notifications)

    observed_types = {event.type for event in events}
    missing = [item.value for item in REQUIRED_EVENT_TYPES if item not in observed_types]
    assert not missing, f"missing required events: {missing}"
    assert len(events) >= 7

    proof = {
        "scenario": "deterministic_reminder_e2e",
        "status": "passed",
        "task_id": task_id,
        "reminder_id": reminder_id,
        "cedar": {
            "version": decision.cedar_version,
            "decision": decision.cedar_decision.value,
            "policy_ids": decision.determining_policy_ids,
        },
        "counts": {
            key: value
            for key, value in before_restart.items()
            if key
            in {
                "intents",
                "tasks",
                "agent_runs",
                "action_requests",
                "policy_decisions",
                "action_results",
                "reminders",
                "notifications",
            }
        },
        "deliveries": ["task_completed", "reminder_due"],
        "restart_duplicate_check": "passed",
    }
    assert proof["counts"] == {
        "intents": 1,
        "tasks": 1,
        "agent_runs": 1,
        "action_requests": 1,
        "policy_decisions": 1,
        "action_results": 1,
        "reminders": 1,
        "notifications": 2,
    }

    PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROOF_PATH.write_text(
        json.dumps(proof, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert os.path.getsize(PROOF_PATH) > 0
