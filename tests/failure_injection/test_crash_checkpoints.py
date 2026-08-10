"""Every checkpoint where a process can die, and exactly what must survive.

Each test crashes once, restarts the roles against the same database, and then asserts record
counts, final states and the absence of any duplicated effect.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.application.execution_service import ExecutionOutcome
from agtyle.bootstrap import Container
from agtyle.domain.actions import ActionStatus
from agtyle.domain.agents import AgentRunStatus
from agtyle.domain.notifications import DeliveryStatus, reminder_due_delivery_key
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import TaskStatus
from agtyle.ports.clock import FrozenClock
from agtyle.ports.failure_injection import Checkpoint
from tests.fakes.failure_injection import InjectedCrash
from tests.integration.conftest import DUE, interaction, record_counts

from .conftest import ContainerFactory, workers


def clock_advance(container: Container) -> None:
    """Let the crashed worker's lease expire so recovery can reclaim its Task."""
    clock = container.clock
    assert isinstance(clock, FrozenClock)
    clock.advance(timedelta(seconds=container.settings.task_lease_seconds + 1))


async def _submit(container: Container) -> str:
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    return result.task_receipt.task_id


async def test_crash_after_claim_leaves_the_attempt_recoverable(
    make_container: ContainerFactory, clock: FrozenClock
) -> None:
    crashing = make_container(Checkpoint.AFTER_TASK_CLAIM_COMMIT)
    task_id = await _submit(crashing)
    worker, _, _ = workers(crashing)

    # A crash is not a graceful failure: nothing cleans up, the process simply stops.
    with pytest.raises(InjectedCrash):
        await worker.run_once()

    counts = await record_counts(crashing)
    assert counts["action_requests"] == 0
    assert counts["reminders"] == 0
    assert counts["agent_runs"] == 1

    # The lease expires; recovery abandons the run and a fresh process finishes the work.
    clock.advance(timedelta(seconds=crashing.settings.task_lease_seconds + 1))
    restarted = make_container(None)
    worker2, _, _ = workers(restarted)
    second = await worker2.run_once()
    assert second is not None
    assert second.outcome is ExecutionOutcome.COMPLETED
    assert second.task_id == task_id

    counts = await record_counts(restarted)
    assert counts["reminders"] == 1
    assert counts["tasks"] == 1


async def test_crash_after_agent_output_leaves_no_action(
    make_container: ContainerFactory,
) -> None:
    crashing = make_container(Checkpoint.AFTER_AGENT_OUTPUT)
    await _submit(crashing)
    worker, _, _ = workers(crashing)

    with pytest.raises(InjectedCrash):
        await worker.run_once()

    counts = await record_counts(crashing)
    assert counts["action_requests"] == 0
    assert counts["policy_decisions"] == 0
    assert counts["reminders"] == 0

    clock_advance(crashing)
    restarted = make_container(None)
    await restarted.recovery_service.recover_expired_leases()
    worker2, _, _ = workers(restarted)
    second = await worker2.run_once()
    assert second is not None
    assert second.outcome is ExecutionOutcome.COMPLETED
    assert (await record_counts(restarted))["reminders"] == 1


async def test_crash_after_policy_decision_reuses_the_same_action(
    make_container: ContainerFactory,
) -> None:
    crashing = make_container(Checkpoint.AFTER_POLICY_DECISION_COMMIT)
    await _submit(crashing)
    worker, _, _ = workers(crashing)

    with pytest.raises(InjectedCrash):
        await worker.run_once()

    after_crash = await record_counts(crashing)
    assert after_crash["action_requests"] == 1
    assert after_crash["policy_decisions"] == 1
    assert after_crash["reminders"] == 0
    assert after_crash["action_results"] == 0

    clock_advance(crashing)
    restarted = make_container(None)
    await restarted.recovery_service.recover_expired_leases()
    worker2, _, _ = workers(restarted)
    second = await worker2.run_once()
    assert second is not None
    assert second.outcome is ExecutionOutcome.COMPLETED

    final = await record_counts(restarted)
    # The retry reuses the same ActionRequest and reauthorizes it rather than proposing a new one.
    assert final["action_requests"] == 1
    assert final["policy_decisions"] == 2
    assert final["action_results"] == 1
    assert final["reminders"] == 1


async def test_crash_after_the_reminder_exists_does_not_duplicate_it(
    make_container: ContainerFactory,
) -> None:
    """The effect is already applied; the completion transaction has not run."""
    crashing = make_container(Checkpoint.AFTER_CAPABILITY_EXECUTION)
    task_id = await _submit(crashing)
    worker, _, _ = workers(crashing)

    with pytest.raises(InjectedCrash):
        await worker.run_once()

    after_crash = await record_counts(crashing)
    assert after_crash["reminders"] == 1
    assert after_crash["action_results"] == 0, "completion must not have committed"
    assert after_crash["notifications"] == 0, "no orphan notification may exist"

    async with crashing.uow_factory() as uow:
        task = await uow.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.RUNNING, "the crashed worker never finalized"

    clock_advance(crashing)
    restarted = make_container(None)
    await restarted.recovery_service.recover_expired_leases()
    worker2, _, _ = workers(restarted)
    second = await worker2.run_once()
    assert second is not None
    assert second.outcome is ExecutionOutcome.COMPLETED

    final = await record_counts(restarted)
    assert final["reminders"] == 1, "the retry observed the prior effect instead of repeating it"
    assert final["action_results"] == 1
    assert final["notifications"] == 1


async def test_crash_after_completion_leaves_the_notification_pending(
    make_container: ContainerFactory, recorder: RecordingNotificationAdapter
) -> None:
    crashing = make_container(Checkpoint.AFTER_COMPLETION_COMMIT)
    task_id = await _submit(crashing)
    worker, _, _ = workers(crashing)

    with pytest.raises(InjectedCrash):
        await worker.run_once()
    # The completion transaction committed before the crash; only delivery is outstanding.
    async with crashing.uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        notifications = await uow.notifications.list_for_task(task_id)
    assert task is not None
    assert task.status is TaskStatus.COMPLETED
    assert len(notifications) == 1
    assert notifications[0].delivery_status is DeliveryStatus.PENDING
    assert recorder.delivered_keys == []

    restarted = make_container(None)
    _, _, notifier = workers(restarted)
    delivered = await notifier.run_once()
    assert delivered is not None
    assert delivered.outcome.value == "delivered"
    assert len(recorder.delivered_keys) == 1


async def test_crash_between_adapter_delivery_and_commit_deduplicates(
    make_container: ContainerFactory, recorder: RecordingNotificationAdapter, clock: FrozenClock
) -> None:
    """The adapter saw it, the database did not. The retry must not deliver twice."""
    crashing = make_container(Checkpoint.AFTER_ADAPTER_DELIVERY)
    task_id = await _submit(crashing)
    worker, _, notifier = workers(crashing)
    await worker.run_once()

    with pytest.raises(InjectedCrash):
        await notifier.run_once()

    assert len(recorder.invocations) == 1
    async with crashing.uow_factory() as uow:
        notifications = await uow.notifications.list_for_task(task_id)
    assert notifications[0].delivery_status is DeliveryStatus.DELIVERING

    # The lease expires, recovery releases it, and a restarted worker retries the same key.
    clock.advance(timedelta(seconds=crashing.settings.notification_lease_seconds + 1))
    restarted = make_container(None)
    await restarted.recovery_service.recover_expired_leases()
    _, _, notifier2 = workers(restarted)
    report = await notifier2.run_once()

    assert report is not None
    assert report.outcome.value == "delivered"
    assert report.duplicate_suppressed, "the adapter must recognize the repeated delivery key"
    assert len(recorder.invocations) == 2
    assert len(recorder.delivered_keys) == 1


async def test_crash_after_firing_leaves_the_due_notification_pending(
    make_container: ContainerFactory, clock: FrozenClock, recorder: RecordingNotificationAdapter
) -> None:
    setup = make_container(None)
    await _submit(setup)
    worker, _, notifier = workers(setup)
    report = await worker.run_once()
    assert report is not None
    await notifier.run_once()

    clock.set(DUE)
    crashing = make_container(Checkpoint.AFTER_REMINDER_FIRING_COMMIT)
    _, scheduler, _ = workers(crashing)
    with pytest.raises(InjectedCrash):
        await scheduler.run_once()

    delivery_key = reminder_due_delivery_key(report.reminder_id or "")
    async with crashing.uow_factory() as uow:
        notification = await uow.notifications.get_by_delivery_key(delivery_key)
        reminder = await uow.reminders.get(report.reminder_id or "")
    assert notification is not None, "the firing transaction committed before the crash"
    assert notification.delivery_status is DeliveryStatus.PENDING
    assert reminder is not None
    assert reminder.status is ReminderStatus.FIRING

    restarted = make_container(None)
    _, scheduler2, notifier2 = workers(restarted)
    assert await scheduler2.run_once() is None, "the reminder must not fire twice"
    delivered = await notifier2.run_once()
    assert delivered is not None
    assert delivered.reminder_id == report.reminder_id

    async with restarted.uow_factory() as uow:
        reminder = await uow.reminders.get(report.reminder_id or "")
        notifications = await uow.notifications.list_for_reminder(report.reminder_id or "")
    assert reminder is not None
    assert reminder.status is ReminderStatus.DELIVERED
    assert len(notifications) == 1
    assert len(recorder.delivered_keys) == 2


async def test_cedar_timeout_prevents_any_capability_call(
    make_container: ContainerFactory,
) -> None:
    from agtyle.adapters.authorization.cedar_cli import CedarCliAuthorization
    from agtyle.application.policy_service import PolicyService
    from agtyle.config import CEDAR_PINNED_VERSION
    from agtyle.domain.actions import CedarDecision, PolicyOutcome
    from agtyle.domain.common import ErrorCode

    container = make_container(None)
    slow = CedarCliAuthorization(
        binary=container.settings.cedar_binary,
        schema=container.settings.cedar_schema,
        policies=container.settings.cedar_policies,
        pinned_version=CEDAR_PINNED_VERSION,
        timeout_seconds=0.000_001,
    )
    container.execution_service._policy = PolicyService(
        registry=container.registry,
        schemas=container.schemas,
        authorization=slow,
        clock=container.clock,
        ids=container.ids,
    )

    await _submit(container)
    worker, _, _ = workers(container)
    report = await worker.run_once()
    assert report is not None
    assert report.error_code is ErrorCode.POLICY_ENGINE_ERROR
    assert report.outcome is ExecutionOutcome.RETRY_SCHEDULED

    counts = await record_counts(container)
    assert counts["reminders"] == 0
    assert counts["action_results"] == 0

    async with container.uow_factory() as uow:
        actions = await uow.actions.list_for_task(report.task_id)
        decisions = await uow.actions.list_policy_decisions(actions[0].id)
    assert decisions[0].decision is PolicyOutcome.ERROR
    assert decisions[0].cedar_decision is CedarDecision.ERROR
    assert actions[0].status is ActionStatus.PROPOSED


async def test_an_old_owner_cannot_finalize_after_losing_its_lease(
    make_container: ContainerFactory, clock: FrozenClock
) -> None:
    container = make_container(None)
    task_id = await _submit(container)

    claimed = await container.execution_service.claim_task(owner="worker-old")
    assert claimed is not None

    # The lease expires and another process recovers the Task while the old owner is still busy.
    clock.advance(timedelta(seconds=container.settings.task_lease_seconds + 1))
    summary = await container.recovery_service.recover_expired_leases()
    assert summary.reassigned_task_ids == [task_id]

    report = await container.execution_service.execute_claimed_task(claimed, owner="worker-old")
    assert report.outcome is ExecutionOutcome.LEASE_LOST

    counts = await record_counts(container)
    assert counts["reminders"] == 0, "the superseded owner must not apply an effect"

    async with container.uow_factory() as uow:
        runs = await uow.agent_runs.list_for_task(task_id)
        task = await uow.tasks.get(task_id)
    assert runs[0].status is AgentRunStatus.ABANDONED
    assert task is not None
    assert task.status is TaskStatus.ASSIGNED

    worker, _, _ = workers(container)
    finished = await worker.run_once()
    assert finished is not None
    assert finished.outcome is ExecutionOutcome.COMPLETED
    assert (await record_counts(container))["reminders"] == 1
