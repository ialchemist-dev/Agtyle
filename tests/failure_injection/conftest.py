"""Failure-injection fixtures: the same container, with a crash wired into one checkpoint."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.bootstrap import Container, build_container, migrate
from agtyle.config import Settings
from agtyle.ports.clock import FrozenClock
from agtyle.ports.failure_injection import Checkpoint
from agtyle.ports.id_generator import DeterministicIdGenerator
from agtyle.workers.notification_worker import NotificationWorker
from agtyle.workers.scheduler import Scheduler
from agtyle.workers.task_worker import TaskWorker
from tests.fakes.failure_injection import CrashAt

ContainerFactory = Callable[[Checkpoint | None], Container]


@pytest.fixture
def recorder() -> RecordingNotificationAdapter:
    return RecordingNotificationAdapter()


@pytest.fixture
def make_container(
    settings: Settings, clock: FrozenClock, recorder: RecordingNotificationAdapter
) -> Iterator[ContainerFactory]:
    """Build containers over one database, so a 'restart' really reuses committed state."""
    migrate(settings)
    built: list[Container] = []

    def factory(crash_at: Checkpoint | None = None, *, times: int = 1) -> Container:
        container = build_container(
            settings,
            clock=clock,
            ids=DeterministicIdGenerator(seed=len(built)),
            notification_adapters={recorder.adapter_name: recorder},
            failures=CrashAt(crash_at, times=times) if crash_at else None,
            configure_logs=False,
        )
        built.append(container)
        return container

    yield factory  # type: ignore[misc]
    for container in built:
        container.dispose()


def workers(container: Container) -> tuple[TaskWorker, Scheduler, NotificationWorker]:
    return (
        TaskWorker(
            execution=container.execution_service,
            recovery=container.recovery_service,
            poll_interval_seconds=0.001,
            lease_seconds=container.settings.task_lease_seconds,
            owner="worker-1",
        ),
        Scheduler(reminders=container.reminder_service, poll_interval_seconds=0.001),
        NotificationWorker(
            notifications=container.notification_service,
            poll_interval_seconds=0.001,
            owner="notifier-1",
        ),
    )
