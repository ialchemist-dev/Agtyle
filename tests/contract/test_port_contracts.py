"""Reusable port contract suites.

Each suite is written against the port, not against one implementation, so a future adapter —
an MCP reminder capability, a Slack notifier, a real secret store — is verified by exactly the
tests the current adapter already passes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agtyle.adapters.capabilities.local_reminders import LocalReminderCapability
from agtyle.adapters.notifications.console import ConsoleNotificationAdapter, render_message
from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.adapters.persistence import migrator
from agtyle.adapters.persistence.database import create_database_engine
from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.adapters.unimplemented import (
    NotImplementedKnowledgeAdapter,
    NotImplementedSecretStoreAdapter,
    NotImplementedWorkflowAdapter,
)
from agtyle.config import Settings
from agtyle.domain.actions import (
    ActionExecutionStatus,
    ActionRequest,
    ActionStatus,
    compute_idempotency_key,
    compute_payload_hash,
)
from agtyle.domain.common import (
    ActionIdempotencyConflictError,
    CapabilityPermanentError,
    ConfigurationInvalidError,
)
from agtyle.domain.intents import Intent
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    task_terminal_delivery_key,
)
from agtyle.domain.tasks import ExecutionMode, Task, TaskOrigin, TaskStatus
from agtyle.ports.capability import CapabilityPort
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator
from agtyle.ports.knowledge import KnowledgeQuery
from agtyle.ports.notification import NotificationPort
from agtyle.ports.secret_store import SecretRef
from agtyle.ports.workflow import WorkflowStart

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
DUE = NOW + timedelta(hours=1)
TASK_ID = "task_00000000-0000-7000-8000-000000000001"
PAYLOAD = {
    "title": "submit the report",
    "note": None,
    "scheduled_for_utc": "2026-08-09T23:00:00Z",
    "timezone": "America/Denver",
}


# --------------------------------------------------------------------------------------
# CapabilityPort
# --------------------------------------------------------------------------------------


@pytest.fixture
def capability(settings: Settings) -> Iterator[tuple[CapabilityPort, SqliteUnitOfWorkFactory]]:
    migrator.upgrade_to_head(settings)
    engine = create_database_engine(settings)
    factory = SqliteUnitOfWorkFactory(engine)
    adapter = LocalReminderCapability(
        uow_factory=factory, clock=FrozenClock(NOW), ids=DeterministicIdGenerator()
    )
    yield adapter, factory
    engine.dispose()


def action(**overrides: Any) -> ActionRequest:
    payload = overrides.pop("payload", PAYLOAD)
    capability_name = overrides.pop("capability", "reminder.create")
    payload_hash = compute_payload_hash(
        principal_agent_id="steward",
        capability=capability_name,
        resource_type="ReminderCollection",
        resource_id="user_local",
        schema_version=overrides.get("schema_version", 1),
        payload=payload,
    )
    base: dict[str, Any] = {
        "id": "act_00000000-0000-7000-8000-000000000001",
        "task_id": TASK_ID,
        "agent_run_id": "run_00000000-0000-7000-8000-000000000001",
        "principal_agent_id": "steward",
        "capability": capability_name,
        "schema_version": 1,
        "resource_type": "ReminderCollection",
        "resource_id": "user_local",
        "payload": payload,
        "payload_hash": payload_hash,
        "idempotency_key": compute_idempotency_key(
            user_id="user_local",
            task_id=TASK_ID,
            capability=capability_name,
            payload_hash=payload_hash,
        ),
        "status": ActionStatus.AUTHORIZED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return ActionRequest(**base)


async def _seed_task(factory: SqliteUnitOfWorkFactory) -> None:
    async with factory() as uow:
        await uow.intents.add(
            Intent(
                id="int_00000000-0000-7000-8000-000000000001",
                user_id="user_local",
                origin_channel="api",
                origin_conversation_id="conv",
                interaction_idempotency_key="k",
                request_hash="sha256:" + "a" * 64,
                original_input="Remind me",
                created_at=NOW,
            )
        )
        await uow.tasks.add(
            Task(
                id=TASK_ID,
                intent_id="int_00000000-0000-7000-8000-000000000001",
                task_type="reminder_create",
                status=TaskStatus.RUNNING,
                execution_mode=ExecutionMode.DELEGATED,
                assigned_agent_id="steward",
                objective="Create a reminder",
                origin=TaskOrigin(channel="api", conversation_id="conv", user_id="user_local"),
                max_attempts=3,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.commit()


async def test_capability_declares_its_name_and_schema_version(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, _ = capability
    assert adapter.capability_name == "reminder.create"
    assert adapter.schema_version == 1


async def test_capability_applies_the_effect_once(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, factory = capability
    await _seed_task(factory)
    result = await adapter.execute(action())
    assert result.status is ActionExecutionStatus.APPLIED
    assert result.external_ref is not None
    assert result.result["reminder_id"] == result.external_ref


async def test_capability_reports_already_applied_for_a_repeated_key(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, factory = capability
    await _seed_task(factory)
    first = await adapter.execute(action())
    second = await adapter.execute(action(id="act_00000000-0000-7000-8000-000000000002"))
    assert second.status is ActionExecutionStatus.ALREADY_APPLIED
    assert second.external_ref == first.external_ref

    async with factory() as uow:
        assert await uow.reminders.count_all() == 1


async def test_capability_raises_on_an_idempotency_conflict(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    """The same key with different protected fields must conflict, never overwrite."""
    adapter, factory = capability
    await _seed_task(factory)
    original = action()
    await adapter.execute(original)

    tampered = original.model_copy(update={"payload": {**PAYLOAD, "title": "something else"}})
    with pytest.raises(ActionIdempotencyConflictError):
        await adapter.execute(tampered)


async def test_capability_rejects_a_foreign_capability_name(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, factory = capability
    await _seed_task(factory)
    with pytest.raises(CapabilityPermanentError):
        await adapter.execute(action(capability="calendar.create_event"))


async def test_capability_revalidates_the_payload_itself(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    """The adapter does not trust that someone upstream validated the payload."""
    adapter, factory = capability
    await _seed_task(factory)
    with pytest.raises(CapabilityPermanentError):
        await adapter.execute(action().model_copy(update={"payload": {}}))


async def test_capability_refuses_a_due_time_in_the_past(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    from agtyle.domain.common import InvalidRequestError

    adapter, factory = capability
    await _seed_task(factory)
    past = {**PAYLOAD, "scheduled_for_utc": "2020-01-01T00:00:00Z"}
    with pytest.raises(InvalidRequestError):
        await adapter.execute(action(payload=past))


async def test_capability_reconciles_a_known_key(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, factory = capability
    await _seed_task(factory)
    request = action()
    async with factory() as uow:
        from agtyle.domain.agents import AgentRun, AgentRunStatus

        await uow.agent_runs.add(
            AgentRun(
                id="run_00000000-0000-7000-8000-000000000001",
                task_id=TASK_ID,
                agent_id="steward",
                agent_version=1,
                attempt=1,
                status=AgentRunStatus.RUNNING,
                context_pack_hash="sha256:" + "c" * 64,
                started_at=NOW,
            )
        )
        await uow.actions.add(request)
        await uow.commit()

    assert not (await adapter.reconcile(request.idempotency_key)).found
    await adapter.execute(request)
    reconciled = await adapter.reconcile(request.idempotency_key)
    assert reconciled.found
    assert reconciled.external_ref is not None


async def test_capability_reconcile_reports_an_unknown_key(
    capability: tuple[CapabilityPort, SqliteUnitOfWorkFactory],
) -> None:
    adapter, _ = capability
    assert not (await adapter.reconcile("never-seen")).found


# --------------------------------------------------------------------------------------
# NotificationPort
# --------------------------------------------------------------------------------------


def notification(**overrides: Any) -> Notification:
    base: dict[str, Any] = {
        "id": "not_00000000-0000-7000-8000-000000000001",
        "task_id": TASK_ID,
        "kind": NotificationKind.TASK_COMPLETED,
        "destination": NotificationDestination(
            adapter="console", user_id="user_local", conversation_id="conv"
        ),
        "payload": {"task_id": TASK_ID, "status": "completed", "result_ref": "rem_1"},
        "delivery_key": task_terminal_delivery_key(TASK_ID, TaskStatus.COMPLETED),
        "delivery_status": DeliveryStatus.DELIVERING,
        "max_attempts": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Notification(**base)


def notification_adapters() -> list[NotificationPort]:
    import io

    return [ConsoleNotificationAdapter(io.StringIO()), RecordingNotificationAdapter()]


@pytest.mark.parametrize("adapter", notification_adapters(), ids=lambda a: a.adapter_name)
async def test_notification_adapter_declares_a_name(adapter: NotificationPort) -> None:
    assert adapter.adapter_name


@pytest.mark.parametrize("adapter", notification_adapters(), ids=lambda a: a.adapter_name)
async def test_notification_adapter_delivers_and_echoes_the_delivery_key(
    adapter: NotificationPort,
) -> None:
    result = await adapter.deliver(notification())
    assert result.delivered
    assert result.external_ref == task_terminal_delivery_key(TASK_ID, TaskStatus.COMPLETED)


@pytest.mark.parametrize("adapter", notification_adapters(), ids=lambda a: a.adapter_name)
async def test_notification_adapter_accepts_every_kind(adapter: NotificationPort) -> None:
    for index, kind in enumerate(NotificationKind):
        payload = {
            "task_id": TASK_ID,
            "status": "completed",
            "error_code": "AGT-TASK-003",
            "title": "stretch",
            "result_ref": "rem_1",
        }
        result = await adapter.deliver(
            notification(
                kind=kind,
                payload=payload,
                delivery_key=f"contract:{kind.value}:{index}",
                reminder_id=(
                    "rem_00000000-0000-7000-8000-000000000001"
                    if kind is NotificationKind.REMINDER_DUE
                    else None
                ),
            )
        )
        assert result.delivered


def test_console_adapter_renders_one_sentence_per_kind() -> None:
    for kind in NotificationKind:
        message = render_message(
            notification(
                kind=kind,
                payload={
                    "task_id": TASK_ID,
                    "status": "completed",
                    "error_code": "AGT-TASK-003",
                    "title": "stretch",
                    "result_ref": "rem_1",
                },
            )
        )
        assert message and message.endswith((".", "stretch"))


def test_console_adapter_writes_one_json_line_with_the_required_fields() -> None:
    import asyncio
    import io
    import json

    stream = io.StringIO()
    adapter = ConsoleNotificationAdapter(stream)
    asyncio.run(adapter.deliver(notification()))
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert set(document) == {
        "notification_id",
        "kind",
        "delivery_key",
        "message",
        "timestamp",
    }


async def test_recording_adapter_suppresses_a_repeated_delivery_key() -> None:
    adapter = RecordingNotificationAdapter()
    first = await adapter.deliver(notification())
    second = await adapter.deliver(notification(id="not_00000000-0000-7000-8000-000000000002"))
    assert first.delivered and second.delivered
    assert second.duplicate_suppressed
    assert len(adapter.delivered_keys) == 1
    assert len(adapter.invocations) == 2


# --------------------------------------------------------------------------------------
# Seams without a production adapter
# --------------------------------------------------------------------------------------


async def test_knowledge_port_fails_with_a_typed_error() -> None:
    with pytest.raises(ConfigurationInvalidError, match="KnowledgePort"):
        await NotImplementedKnowledgeAdapter().retrieve(KnowledgeQuery(text="anything"))


async def test_secret_store_port_fails_with_a_typed_error() -> None:
    with pytest.raises(ConfigurationInvalidError, match="SecretStorePort"):
        await NotImplementedSecretStoreAdapter().resolve(SecretRef(name="gmail_oauth"))


async def test_workflow_port_fails_with_a_typed_error() -> None:
    with pytest.raises(ConfigurationInvalidError, match="WorkflowEnginePort"):
        await NotImplementedWorkflowAdapter().start(
            WorkflowStart(workflow_name="wait", workflow_id="w1")
        )


@pytest.mark.parametrize(
    "adapter",
    [
        NotImplementedKnowledgeAdapter(),
        NotImplementedSecretStoreAdapter(),
        NotImplementedWorkflowAdapter(),
    ],
    ids=lambda a: type(a).__name__,
)
def test_unimplemented_adapters_never_look_like_success(adapter: Any) -> None:
    """A missing adapter must be a typed failure, never a silent empty result."""
    assert adapter.adapter_name == "not_implemented"
