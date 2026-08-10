"""Integration fixtures: a real database file, the real Cedar engine, a frozen clock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.application.interaction_service import HandleInteraction
from agtyle.bootstrap import Container, build_container, migrate
from agtyle.config import Settings
from agtyle.domain.tasks import ExecutionMode
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator
from agtyle.workers.notification_worker import NotificationWorker
from agtyle.workers.scheduler import Scheduler
from agtyle.workers.task_worker import TaskWorker

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
DUE = datetime(2026, 8, 9, 22, 5, tzinfo=UTC)
DEFAULT_INPUT = "Remind me to submit the report at 2026-08-09T22:05:00Z"


@pytest.fixture
def recorder() -> RecordingNotificationAdapter:
    return RecordingNotificationAdapter()


@pytest.fixture
def container(
    settings: Settings, clock: FrozenClock, recorder: RecordingNotificationAdapter
) -> Iterator[Container]:
    migrate(settings)
    built = build_container(
        settings,
        clock=clock,
        ids=DeterministicIdGenerator(),
        notification_adapters={recorder.adapter_name: recorder},
        configure_logs=False,
    )
    yield built
    built.dispose()


@pytest.fixture
def task_worker(container: Container) -> TaskWorker:
    return TaskWorker(
        execution=container.execution_service,
        recovery=container.recovery_service,
        poll_interval_seconds=0.001,
        lease_seconds=container.settings.task_lease_seconds,
        owner="worker-1",
    )


@pytest.fixture
def scheduler(container: Container) -> Scheduler:
    return Scheduler(reminders=container.reminder_service, poll_interval_seconds=0.001)


@pytest.fixture
def notification_worker(container: Container) -> NotificationWorker:
    return NotificationWorker(
        notifications=container.notification_service,
        poll_interval_seconds=0.001,
        owner="notifier-1",
    )


def interaction(
    *,
    key: str = "e2e-reminder-001",
    text: str = DEFAULT_INPUT,
    mode: ExecutionMode = ExecutionMode.DELEGATED,
    user_id: str = "user_local",
) -> HandleInteraction:
    return HandleInteraction(
        user_id=user_id,
        conversation_id="conv_e2e",
        channel="api",
        input=text,
        idempotency_key=key,
        preferred_execution_mode=mode,
    )


async def record_counts(container: Container) -> dict[str, int]:
    async with container.uow_factory() as uow:
        return {
            "intents": await uow.intents.count_all(),
            "tasks": await uow.tasks.count_all(),
            "agent_runs": await uow.agent_runs.count_all(),
            "action_requests": await uow.actions.count_requests(),
            "policy_decisions": await uow.actions.count_decisions(),
            "action_results": await uow.actions.count_results(),
            "reminders": await uow.reminders.count_all(),
            "notifications": await uow.notifications.count_all(),
            "approvals": await uow.approvals.count_all(),
            "events": await uow.events.count_all(),
        }
