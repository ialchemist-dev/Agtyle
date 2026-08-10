"""Round-tripping and idempotency guarantees at the repository boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.domain.actions import (
    ActionExecutionStatus,
    ActionResult,
    ActionStatus,
    CedarDecision,
    PolicyDecision,
    PolicyOutcome,
)
from agtyle.domain.agents import AgentRunStatus
from agtyle.domain.events import Event, EventType
from agtyle.domain.notifications import DeliveryStatus
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import TaskStatus
from agtyle.ports.repositories import ConcurrentUpdateError

from .conftest import (
    ACTION_ID,
    DUE,
    NOW,
    TASK_ID,
    make_action_request,
    make_agent_run,
    make_intent,
    make_notification,
    make_reminder,
    make_task,
    seed_task,
)


async def test_intent_round_trips_without_losing_the_original_input(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    original = make_intent(original_input="Remind me to 提交报告 at 2026-08-09T22:05:00Z")
    async with uow_factory() as uow:
        await uow.intents.add(original)
        await uow.commit()

    async with uow_factory() as uow:
        loaded = await uow.intents.get(original.id)
    assert loaded == original
    assert loaded is not None
    assert loaded.original_input == original.original_input


async def test_task_round_trips_with_type_and_identity_intact(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    task = await seed_task(uow_factory)
    async with uow_factory() as uow:
        loaded = await uow.tasks.get(task.id)
    assert loaded == task
    assert loaded is not None
    assert loaded.status is TaskStatus.ASSIGNED
    assert loaded.origin.user_id == "user_local"


async def test_duplicate_interaction_idempotency_key_is_rejected(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await uow.intents.add(make_intent())
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.intents.add(make_intent(id="int_00000000-0000-7000-8000-000000000002"))
            await uow.commit()


async def test_optimistic_update_rejects_a_stale_writer(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    task = await seed_task(uow_factory)
    running = task.start(lease_owner="worker-1", now=NOW, lease_seconds=30)
    async with uow_factory() as uow:
        await uow.tasks.update(running, expected_row_version=task.row_version)
        await uow.commit()

    stale = task.start(lease_owner="worker-2", now=NOW, lease_seconds=30)
    with pytest.raises(ConcurrentUpdateError):
        async with uow_factory() as uow:
            await uow.tasks.update(stale, expected_row_version=task.row_version)
            await uow.commit()


async def test_agent_run_attempt_is_unique_per_task(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.agent_runs.add(make_agent_run())
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.agent_runs.add(make_agent_run(id="run_00000000-0000-7000-8000-000000000002"))
            await uow.commit()


async def test_agent_run_lifecycle_persists(uow_factory: SqliteUnitOfWorkFactory) -> None:
    await seed_task(uow_factory)
    run = make_agent_run()
    async with uow_factory() as uow:
        await uow.agent_runs.add(run)
        await uow.agent_runs.update(run.succeed(now=NOW + timedelta(seconds=1)))
        await uow.commit()

    async with uow_factory() as uow:
        runs = await uow.agent_runs.list_for_task(TASK_ID)
    assert [r.status for r in runs] == [AgentRunStatus.SUCCEEDED]


async def test_duplicate_action_idempotency_key_is_rejected(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.agent_runs.add(make_agent_run())
        await uow.actions.add(make_action_request())
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.actions.add(
                make_action_request(id="act_00000000-0000-7000-8000-000000000002")
            )
            await uow.commit()


async def test_action_result_is_unique_per_action_request(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.agent_runs.add(make_agent_run())
        await uow.actions.add(make_action_request())
        await uow.actions.add_result(
            ActionResult(
                id="res_00000000-0000-7000-8000-000000000001",
                action_request_id=ACTION_ID,
                status=ActionExecutionStatus.APPLIED,
                result={"reminder_id": "rem_1"},
                started_at=NOW,
                completed_at=NOW,
                created_at=NOW,
            )
        )
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.actions.add_result(
                ActionResult(
                    id="res_00000000-0000-7000-8000-000000000002",
                    action_request_id=ACTION_ID,
                    status=ActionExecutionStatus.APPLIED,
                    result={},
                    started_at=NOW,
                    completed_at=NOW,
                    created_at=NOW,
                )
            )
            await uow.commit()


async def test_policy_decision_retains_the_normalized_request(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    request = {"principal": 'Agtyle::Agent::"steward"', "context": {"approval_present": False}}
    async with uow_factory() as uow:
        await uow.agent_runs.add(make_agent_run())
        await uow.actions.add(make_action_request())
        await uow.actions.add_policy_decision(
            PolicyDecision(
                id="pol_00000000-0000-7000-8000-000000000001",
                action_request_id=ACTION_ID,
                decision=PolicyOutcome.ALLOW,
                cedar_decision=CedarDecision.ALLOW,
                reason_code="cedar_allow",
                determining_policy_ids=["permit-steward-direct-reminder"],
                request=request,
                cedar_version="4.12.0",
                created_at=NOW,
            )
        )
        await uow.commit()

    async with uow_factory() as uow:
        decisions = await uow.actions.list_policy_decisions(ACTION_ID)
    assert len(decisions) == 1
    assert decisions[0].request == request
    assert decisions[0].determining_policy_ids == ["permit-steward-direct-reminder"]


async def test_reminder_idempotency_key_is_unique_per_user(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.reminders.add(make_reminder())
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.reminders.add(
                make_reminder(id="rem_00000000-0000-7000-8000-000000000002", title="other")
            )
            await uow.commit()


async def test_reminder_lookup_by_idempotency_key_returns_the_existing_record(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    reminder = make_reminder()
    async with uow_factory() as uow:
        await uow.reminders.add(reminder)
        await uow.commit()

    async with uow_factory() as uow:
        found = await uow.reminders.get_by_idempotency_key(
            user_id="user_local", key=reminder.idempotency_key
        )
    assert found == reminder
    assert found is not None
    assert found.matches_protected_fields(reminder)


async def test_notification_delivery_key_is_unique(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.notifications.add(make_notification())
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.notifications.add(
                make_notification(id="not_00000000-0000-7000-8000-000000000002")
            )
            await uow.commit()


async def test_notification_round_trips_with_structured_destination(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    notification = make_notification()
    async with uow_factory() as uow:
        await uow.notifications.add(notification)
        await uow.commit()

    async with uow_factory() as uow:
        loaded = await uow.notifications.get_by_delivery_key(notification.delivery_key)
    assert loaded == notification
    assert loaded is not None
    assert loaded.destination.adapter == "recording"


async def test_rollback_leaves_no_partial_task_or_notification(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await uow.intents.add(make_intent())
        await uow.tasks.add(make_task())
        await uow.notifications.add(make_notification())
        await uow.rollback()

    async with uow_factory() as uow:
        assert await uow.tasks.get(TASK_ID) is None
        assert await uow.intents.get(make_intent().id) is None
        assert await uow.notifications.get_by_delivery_key(make_notification().delivery_key) is None


async def test_unhandled_failure_inside_the_unit_of_work_rolls_back(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async with uow_factory() as uow:
            await uow.intents.add(make_intent())
            await uow.tasks.add(make_task())
            raise Boom("failure after partial writes")

    async with uow_factory() as uow:
        assert await uow.tasks.get(TASK_ID) is None


async def test_events_are_appended_and_queried_in_order(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        for index, event_type in enumerate(
            [EventType.INTENT_ACCEPTED, EventType.TASK_ASSIGNED, EventType.TASK_STARTED]
        ):
            await uow.events.append(
                Event(
                    id=f"evt_00000000-0000-7000-8000-00000000000{index + 1}",
                    type=event_type,
                    subject=TASK_ID,
                    actor="system:kernel",
                    time=NOW + timedelta(seconds=index),
                    data={"index": index},
                )
            )
        await uow.commit()

    async with uow_factory() as uow:
        events = await uow.events.list_by_subject(TASK_ID)
    assert [event.type for event in events] == [
        EventType.INTENT_ACCEPTED,
        EventType.TASK_ASSIGNED,
        EventType.TASK_STARTED,
    ]


async def test_action_status_transitions_persist(uow_factory: SqliteUnitOfWorkFactory) -> None:
    await seed_task(uow_factory)
    action = make_action_request()
    async with uow_factory() as uow:
        await uow.agent_runs.add(make_agent_run())
        await uow.actions.add(action)
        await uow.commit()

    authorized = action.with_status(ActionStatus.AUTHORIZED, now=NOW)
    async with uow_factory() as uow:
        await uow.actions.update(authorized, expected_row_version=action.row_version)
        await uow.commit()

    async with uow_factory() as uow:
        loaded = await uow.actions.get(action.id)
    assert loaded is not None
    assert loaded.status is ActionStatus.AUTHORIZED
    assert loaded.hash_matches()


async def test_reminder_status_and_lease_persist(uow_factory: SqliteUnitOfWorkFactory) -> None:
    await seed_task(uow_factory)
    reminder = make_reminder()
    async with uow_factory() as uow:
        await uow.reminders.add(reminder)
        await uow.commit()

    firing = reminder.begin_firing(
        now=DUE, lease_owner="scheduler-1", lease_expires_at=DUE + timedelta(seconds=30)
    )
    async with uow_factory() as uow:
        await uow.reminders.update(firing, expected_row_version=reminder.row_version)
        await uow.commit()

    async with uow_factory() as uow:
        loaded = await uow.reminders.get(reminder.id)
    assert loaded is not None
    assert loaded.status is ReminderStatus.FIRING
    assert loaded.firing_lease_owner == "scheduler-1"


async def test_notification_claim_marks_it_delivering(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.notifications.add(make_notification())
        await uow.commit()

    async with uow_factory() as uow:
        claimed = await uow.notifications.claim_next_pending(
            owner="notifier-1", now=NOW, lease_seconds=30
        )
        await uow.commit()
    assert claimed is not None
    assert claimed.delivery_status is DeliveryStatus.DELIVERING
    assert claimed.attempt_count == 1
