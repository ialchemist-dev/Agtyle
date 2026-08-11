"""Negative authorization paths. A Capability must never run without a real ALLOW."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agtyle.adapters.agent_runtimes.deterministic_steward import DeterministicStewardRuntime
from agtyle.adapters.authorization.cedar_cli import CedarCliAuthorization
from agtyle.application.execution_service import ExecutionOutcome
from agtyle.application.policy_service import PolicyService
from agtyle.bootstrap import Container
from agtyle.config import CEDAR_PINNED_VERSION
from agtyle.domain.actions import ActionStatus, CedarDecision, PolicyOutcome
from agtyle.domain.agents import (
    ActionRequestProposal,
    AgentRunStatus,
    ClarificationResponse,
    DirectResponse,
)
from agtyle.domain.common import ErrorCode
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import TaskStatus
from agtyle.ports.capability import ActionExecutionResult
from agtyle.workers.task_worker import TaskWorker
from tests.fakes.runtimes import ScriptedRuntime

from .conftest import interaction, record_counts


class RecordingCapability:
    """Wraps the real capability so a test can prove it was never invoked."""

    capability_name = "reminder.create"
    schema_version = 1

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    async def execute(self, request: object) -> ActionExecutionResult:
        self.calls += 1
        return await self._inner.execute(request)  # type: ignore[attr-defined, no-any-return]

    async def reconcile(self, idempotency_key: str) -> object:
        return await self._inner.reconcile(idempotency_key)  # type: ignore[attr-defined]


@pytest.fixture
def spy_capability(container: Container) -> RecordingCapability:
    spy = RecordingCapability(container.capabilities["reminder.create"])
    container.capabilities["reminder.create"] = spy  # type: ignore[assignment]
    return spy


async def test_research_agent_is_denied_reminder_capability(
    container: Container, task_worker: TaskWorker, spy_capability: RecordingCapability
) -> None:
    """An Agent the manifest does not authorize is stopped before Cedar and before the adapter."""
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        ActionRequestProposal(
            capability="reminder.create",
            schema_version=1,
            resource_type="ReminderCollection",
            resource_id="user_local",
            payload={
                "title": "submit the report",
                "note": None,
                "scheduled_for_utc": "2026-08-09T22:05:00Z",
                "timezone": "America/Denver",
            },
        )
    )
    # Re-point the task at an agent that exists but may not request this capability.
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    async with container.uow_factory() as uow:
        task = await uow.tasks.get(result.task_receipt.task_id)
        assert task is not None
        await uow.tasks.update(
            task.model_copy(
                update={"assigned_agent_id": "executive", "row_version": task.row_version + 1}
            ),
            expected_row_version=task.row_version,
        )
        await uow.commit()
    container.agent_runtimes["executive"] = container.agent_runtimes["steward"]

    report = await task_worker.run_once()
    assert report is not None
    assert report.outcome is ExecutionOutcome.FAILED
    assert report.error_code is ErrorCode.POLICY_DENIED
    assert spy_capability.calls == 0

    counts = await record_counts(container)
    assert counts["reminders"] == 0
    assert counts["action_results"] == 0
    assert counts["policy_decisions"] == 1


async def test_missing_cedar_fails_closed_without_reminder(
    container: Container,
    task_worker: TaskWorker,
    spy_capability: RecordingCapability,
    tmp_path: Path,
) -> None:
    """No binary means no decision, and no decision means no effect."""
    broken = CedarCliAuthorization(
        binary=tmp_path / "absent-cedar",
        schema=container.settings.cedar_schema,
        policies=container.settings.cedar_policies,
        pinned_version=CEDAR_PINNED_VERSION,
    )
    container.execution_service._policy = PolicyService(
        registry=container.registry,
        schemas=container.schemas,
        authorization=broken,
        clock=container.clock,
        ids=container.ids,
    )

    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    assert report.error_code is ErrorCode.POLICY_ENGINE_ERROR
    assert spy_capability.calls == 0

    counts = await record_counts(container)
    assert counts["reminders"] == 0
    assert counts["action_results"] == 0

    async with container.uow_factory() as uow:
        actions = await uow.actions.list_for_task(report.task_id)
        decisions = await uow.actions.list_policy_decisions(actions[0].id)
        task = await uow.tasks.get(report.task_id)
    assert decisions[0].decision is PolicyOutcome.ERROR
    assert decisions[0].cedar_decision is CedarDecision.ERROR
    assert task is not None
    # An engine error is retryable, so the Task is queued again rather than failed outright.
    assert task.status is TaskStatus.ASSIGNED
    assert report.outcome is ExecutionOutcome.RETRY_SCHEDULED


async def test_invalid_agent_output_fails_task_without_action(
    container: Container, task_worker: TaskWorker, spy_capability: RecordingCapability
) -> None:
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        DirectResponse(message="I already did it, trust me.")
    )
    await container.interaction_service.handle(interaction())

    report = await task_worker.run_once()
    assert report is not None
    assert report.error_code is ErrorCode.INVALID_AGENT_OUTPUT
    assert spy_capability.calls == 0

    counts = await record_counts(container)
    assert counts["action_requests"] == 0
    assert counts["policy_decisions"] == 0
    assert counts["reminders"] == 0

    async with container.uow_factory() as uow:
        runs = await uow.agent_runs.list_for_task(report.task_id)
    assert runs[0].status is AgentRunStatus.FAILED


async def test_an_agent_asking_for_clarification_mid_task_does_not_act(
    container: Container, task_worker: TaskWorker, spy_capability: RecordingCapability
) -> None:
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        ClarificationResponse(message="Which timezone?", missing=["timezone"])
    )
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    assert report.error_code is ErrorCode.INVALID_AGENT_OUTPUT
    assert spy_capability.calls == 0


async def test_agent_supplied_control_values_are_ignored(
    container: Container, task_worker: TaskWorker
) -> None:
    """An Agent cannot smuggle a principal, status or idempotency key into the payload."""
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        ActionRequestProposal(
            capability="reminder.create",
            schema_version=1,
            resource_type="ReminderCollection",
            resource_id="user_local",
            payload={
                "title": "submit the report",
                "note": None,
                "scheduled_for_utc": "2026-08-09T22:05:00Z",
                "timezone": "America/Denver",
            },
        )
    )
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    report = await task_worker.run_once()
    assert report is not None

    async with container.uow_factory() as uow:
        action = await uow.actions.get(report.action_request_id or "")
    assert action is not None
    assert action.principal_agent_id == "steward"
    assert action.status is ActionStatus.APPLIED
    assert set(action.payload) == {"title", "note", "scheduled_for_utc", "timezone"}
    assert action.hash_matches()


async def test_a_payload_that_violates_the_schema_never_reaches_cedar(
    container: Container, task_worker: TaskWorker, spy_capability: RecordingCapability
) -> None:
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        ActionRequestProposal(
            capability="reminder.create",
            schema_version=1,
            resource_type="ReminderCollection",
            resource_id="user_local",
            payload={"title": "", "scheduled_for_utc": "not-a-time", "timezone": "Nowhere/Land"},
        )
    )
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    assert report.error_code is ErrorCode.ACTION_SCHEMA_INVALID
    assert spy_capability.calls == 0

    counts = await record_counts(container)
    assert counts["policy_decisions"] == 0
    assert counts["reminders"] == 0


async def test_a_reminder_in_the_past_is_rejected_by_the_capability(
    container: Container, task_worker: TaskWorker
) -> None:
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        ActionRequestProposal(
            capability="reminder.create",
            schema_version=1,
            resource_type="ReminderCollection",
            resource_id="user_local",
            payload={
                "title": "already gone",
                "note": None,
                "scheduled_for_utc": "2020-01-01T00:00:00Z",
                "timezone": "America/Denver",
            },
        )
    )
    await container.interaction_service.handle(interaction())
    report = await task_worker.run_once()
    assert report is not None
    assert report.outcome is ExecutionOutcome.FAILED

    counts = await record_counts(container)
    assert counts["reminders"] == 0
    # Cedar decided authority; the domain decided the data was wrong. Both were consulted.
    assert counts["policy_decisions"] == 1


async def test_retrying_the_same_action_creates_one_reminder(
    container: Container, task_worker: TaskWorker
) -> None:
    """A second attempt with the same protected payload reuses the effect, never duplicates it."""
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    first = await task_worker.run_once()
    assert first is not None

    # Force a second attempt over the same Task by returning it to `assigned`.
    async with container.uow_factory() as uow:
        task = await uow.tasks.get(first.task_id)
        assert task is not None
        reopened = task.model_copy(
            update={
                "status": TaskStatus.ASSIGNED,
                "attempt_count": 1,
                "row_version": task.row_version + 1,
            }
        )
        await uow.tasks.update(reopened, expected_row_version=task.row_version)
        await uow.commit()

    second = await task_worker.run_once()
    assert second is not None
    assert second.reminder_id == first.reminder_id

    counts = await record_counts(container)
    assert counts["reminders"] == 1
    assert counts["action_requests"] == 1
    assert counts["action_results"] == 1
    assert counts["agent_runs"] == 2

    async with container.uow_factory() as uow:
        reminder = await uow.reminders.get(first.reminder_id or "")
    assert reminder is not None
    assert reminder.status is ReminderStatus.SCHEDULED


async def test_interactive_timeout_converts_same_task_to_delegated(
    container: Container, task_worker: TaskWorker
) -> None:
    from agtyle.domain.tasks import ExecutionMode
    from tests.fakes.runtimes import SlowRuntime

    proposal = ActionRequestProposal(
        capability="reminder.create",
        schema_version=1,
        resource_type="ReminderCollection",
        resource_id="user_local",
        payload={
            "title": "submit the report",
            "note": None,
            "scheduled_for_utc": "2026-08-09T22:05:00Z",
            "timezone": "America/Denver",
        },
    )
    container.agent_runtimes["steward"] = SlowRuntime(proposal, delay_seconds=5.0)  # type: ignore[assignment]
    container.interaction_service._interactive_budget = 0.05

    result = await container.interaction_service.handle(interaction(mode=ExecutionMode.INTERACTIVE))
    assert result.task_receipt is not None
    task_id = result.task_receipt.task_id

    async with container.uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        tasks = await uow.tasks.list_by_intent(result.intent_id)
    assert task is not None
    assert task.id == task_id, "conversion must not create a second task"
    assert len(tasks) == 1
    assert task.execution_mode is ExecutionMode.DELEGATED
    assert task.status is TaskStatus.ASSIGNED

    # A Worker can now pick up the very same Task.
    container.agent_runtimes["steward"] = DeterministicStewardRuntime(
        default_timezone=container.settings.local_timezone
    )
    report = await task_worker.run_once()
    assert report is not None
    assert report.task_id == task_id
    assert report.outcome is ExecutionOutcome.COMPLETED


async def test_a_denied_task_still_records_a_terminal_notification(
    container: Container, task_worker: TaskWorker
) -> None:
    container.agent_runtimes["steward"] = ScriptedRuntime(  # type: ignore[assignment]
        DirectResponse(message="nope")
    )
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    report = await task_worker.run_once()
    assert report is not None

    async with container.uow_factory() as uow:
        notifications = await uow.notifications.list_for_task(report.task_id)
    assert len(notifications) == 1
    assert notifications[0].kind.value == "task_failed"
    assert notifications[0].payload["error_code"] == ErrorCode.INVALID_AGENT_OUTPUT.value
    assert "traceback" not in str(notifications[0].payload).lower()


async def test_lease_expiry_recovers_a_task_left_running(
    container: Container, task_worker: TaskWorker
) -> None:
    from agtyle.ports.clock import FrozenClock

    clock = container.clock
    assert isinstance(clock, FrozenClock)

    await container.interaction_service.handle(interaction())
    claimed = await container.execution_service.claim_task(owner="crashed-worker")
    assert claimed is not None

    # The worker dies here. Its lease expires without any finalization.
    clock.advance(timedelta(seconds=container.settings.task_lease_seconds + 1))

    summary = await container.recovery_service.recover_expired_leases()
    assert summary.reassigned_task_ids == [claimed.task.id]

    async with container.uow_factory() as uow:
        runs = await uow.agent_runs.list_for_task(claimed.task.id)
        task = await uow.tasks.get(claimed.task.id)
    assert runs[0].status is AgentRunStatus.ABANDONED
    assert task is not None
    assert task.status is TaskStatus.ASSIGNED

    report = await task_worker.run_once()
    assert report is not None
    assert report.outcome is ExecutionOutcome.COMPLETED

    counts = await record_counts(container)
    assert counts["reminders"] == 1
    assert counts["agent_runs"] == 2
