"""Persistence fixtures: a real temporary SQLite file, migrated exactly as production is."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from agtyle.adapters.persistence import migrator
from agtyle.adapters.persistence.database import create_database_engine
from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.config import Settings
from agtyle.domain.actions import (
    ActionRequest,
    ActionStatus,
    compute_idempotency_key,
    compute_payload_hash,
)
from agtyle.domain.agents import AgentRun, AgentRunStatus
from agtyle.domain.intents import Intent
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    task_terminal_delivery_key,
)
from agtyle.domain.reminders import Reminder, ReminderStatus
from agtyle.domain.tasks import ExecutionMode, Task, TaskOrigin, TaskStatus

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
DUE = datetime(2026, 8, 9, 22, 5, tzinfo=UTC)

INTENT_ID = "int_00000000-0000-7000-8000-000000000001"
TASK_ID = "task_00000000-0000-7000-8000-000000000001"
RUN_ID = "run_00000000-0000-7000-8000-000000000001"
ACTION_ID = "act_00000000-0000-7000-8000-000000000001"
REMINDER_ID = "rem_00000000-0000-7000-8000-000000000001"


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrator.upgrade_to_head(settings)
    created = create_database_engine(settings)
    yield created
    created.dispose()


@pytest.fixture
def uow_factory(engine: Engine) -> SqliteUnitOfWorkFactory:
    return SqliteUnitOfWorkFactory(engine)


def make_intent(**overrides: object) -> Intent:
    base: dict[str, object] = {
        "id": INTENT_ID,
        "user_id": "user_local",
        "origin_channel": "api",
        "origin_conversation_id": "conv_demo",
        "interaction_idempotency_key": "e2e-reminder-001",
        "request_hash": "sha256:" + "a" * 64,
        "original_input": "Remind me to submit the report at 2026-08-09T22:05:00Z",
        "created_at": NOW,
    }
    base.update(overrides)
    return Intent(**base)  # type: ignore[arg-type]


def make_task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": TASK_ID,
        "intent_id": INTENT_ID,
        "task_type": "reminder_create",
        "status": TaskStatus.ASSIGNED,
        "execution_mode": ExecutionMode.DELEGATED,
        "assigned_agent_id": "steward",
        "objective": "Create a reminder",
        "payload": {"title": "submit the report"},
        "origin": TaskOrigin(channel="api", conversation_id="conv_demo", user_id="user_local"),
        "max_attempts": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def make_agent_run(**overrides: object) -> AgentRun:
    base: dict[str, object] = {
        "id": RUN_ID,
        "task_id": TASK_ID,
        "agent_id": "steward",
        "agent_version": 1,
        "attempt": 1,
        "status": AgentRunStatus.RUNNING,
        "context_pack_hash": "sha256:" + "c" * 64,
        "started_at": NOW,
    }
    base.update(overrides)
    return AgentRun(**base)  # type: ignore[arg-type]


REMINDER_PAYLOAD = {
    "title": "submit the report",
    "note": None,
    "scheduled_for_utc": "2026-08-09T22:05:00Z",
    "timezone": "America/Denver",
}


def make_action_request(**overrides: object) -> ActionRequest:
    payload = overrides.pop("payload", REMINDER_PAYLOAD)
    payload_hash = compute_payload_hash(
        principal_agent_id="steward",
        capability="reminder.create",
        resource_type="ReminderCollection",
        resource_id="user_local",
        schema_version=1,
        payload=payload,  # type: ignore[arg-type]
    )
    base: dict[str, object] = {
        "id": ACTION_ID,
        "task_id": TASK_ID,
        "agent_run_id": RUN_ID,
        "principal_agent_id": "steward",
        "capability": "reminder.create",
        "schema_version": 1,
        "resource_type": "ReminderCollection",
        "resource_id": "user_local",
        "payload": payload,
        "payload_hash": payload_hash,
        "idempotency_key": compute_idempotency_key(
            user_id="user_local",
            task_id=TASK_ID,
            capability="reminder.create",
            payload_hash=payload_hash,
        ),
        "status": ActionStatus.PROPOSED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return ActionRequest(**base)  # type: ignore[arg-type]


def make_reminder(**overrides: object) -> Reminder:
    base: dict[str, object] = {
        "id": REMINDER_ID,
        "user_id": "user_local",
        "source_task_id": TASK_ID,
        "title": "submit the report",
        "note": None,
        "scheduled_for_utc": DUE,
        "timezone": "America/Denver",
        "status": ReminderStatus.SCHEDULED,
        "idempotency_key": "k" * 64,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Reminder(**base)  # type: ignore[arg-type]


def make_notification(**overrides: object) -> Notification:
    base: dict[str, object] = {
        "id": "not_00000000-0000-7000-8000-000000000001",
        "task_id": TASK_ID,
        "kind": NotificationKind.TASK_COMPLETED,
        "destination": NotificationDestination(
            adapter="recording", user_id="user_local", conversation_id="conv_demo"
        ),
        "payload": {"task_id": TASK_ID, "status": "completed"},
        "delivery_key": task_terminal_delivery_key(TASK_ID, TaskStatus.COMPLETED),
        "delivery_status": DeliveryStatus.PENDING,
        "max_attempts": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Notification(**base)  # type: ignore[arg-type]


async def seed_task(factory: SqliteUnitOfWorkFactory, **task_overrides: object) -> Task:
    task = make_task(**task_overrides)
    async with factory() as uow:
        await uow.intents.add(make_intent())
        await uow.tasks.add(task)
        await uow.commit()
    return task
