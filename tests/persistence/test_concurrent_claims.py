"""Concurrency proofs.

SQLite offers no ``SKIP LOCKED``, so exclusion comes from conditional updates inside
``BEGIN IMMEDIATE`` transactions. These tests run real threads against a real database file
rather than simulating the race, because the guarantee being verified is a database guarantee.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import Engine, text

from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    reminder_due_delivery_key,
)
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import TaskStatus

from .conftest import DUE, NOW, make_notification, make_reminder, seed_task


def _run_concurrently(work: list[object]) -> list[object]:
    """Execute independent coroutine factories on separate threads and event loops."""

    def runner(factory: object) -> object:
        return asyncio.run(factory())  # type: ignore[operator, no-any-return]

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        return list(pool.map(runner, work))


async def test_two_workers_claim_only_one_task(
    engine: Engine, uow_factory: SqliteUnitOfWorkFactory
) -> None:
    await seed_task(uow_factory)

    def claim(owner: str) -> object:
        async def run() -> str | None:
            async with uow_factory() as uow:
                claimed = await uow.tasks.claim_next_assigned(
                    owner=owner, now=NOW, lease_seconds=30
                )
                await uow.commit()
                return claimed.id if claimed else None

        return run

    results = _run_concurrently([claim("worker-1"), claim("worker-2")])
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1, f"expected exactly one claim, got {results}"

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT status, attempt_count, lease_owner FROM tasks")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == TaskStatus.RUNNING.value
    assert rows[0].attempt_count == 1


async def test_two_schedulers_create_one_due_notification(
    engine: Engine, uow_factory: SqliteUnitOfWorkFactory
) -> None:
    await seed_task(uow_factory)
    reminder = make_reminder()
    async with uow_factory() as uow:
        await uow.reminders.add(reminder)
        await uow.commit()

    def fire(owner: str, notification_id: str) -> object:
        async def run() -> str | None:
            async with uow_factory() as uow:
                claimed = await uow.reminders.claim_next_due(owner=owner, now=DUE, lease_seconds=30)
                if claimed is None:
                    return None
                await uow.notifications.add(
                    Notification(
                        id=notification_id,
                        reminder_id=claimed.id,
                        kind=NotificationKind.REMINDER_DUE,
                        destination=NotificationDestination(
                            adapter="recording",
                            user_id=claimed.user_id,
                            conversation_id="conv_demo",
                        ),
                        payload={"reminder_id": claimed.id},
                        delivery_key=reminder_due_delivery_key(claimed.id),
                        delivery_status=DeliveryStatus.PENDING,
                        max_attempts=3,
                        created_at=DUE,
                        updated_at=DUE,
                    )
                )
                await uow.commit()
                return claimed.id

        return run

    results = _run_concurrently(
        [
            fire("scheduler-1", "not_00000000-0000-7000-8000-000000000011"),
            fire("scheduler-2", "not_00000000-0000-7000-8000-000000000012"),
        ]
    )
    assert len([result for result in results if result is not None]) == 1

    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM notifications")).scalar_one()
        status = connection.execute(text("SELECT status FROM reminders")).scalar_one()
    assert count == 1
    assert status == ReminderStatus.FIRING.value


async def test_two_notification_workers_deliver_one_record(
    engine: Engine, uow_factory: SqliteUnitOfWorkFactory
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.notifications.add(make_notification())
        await uow.commit()

    def claim(owner: str) -> object:
        async def run() -> str | None:
            async with uow_factory() as uow:
                claimed = await uow.notifications.claim_next_pending(
                    owner=owner, now=NOW, lease_seconds=30
                )
                await uow.commit()
                return claimed.id if claimed else None

        return run

    results = _run_concurrently([claim("notifier-1"), claim("notifier-2")])
    assert len([result for result in results if result is not None]) == 1

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT delivery_status, attempt_count FROM notifications")
        ).one()
    assert row.delivery_status == DeliveryStatus.DELIVERING.value
    assert row.attempt_count == 1


async def test_a_task_not_yet_retryable_is_not_claimed(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    task = await seed_task(uow_factory)
    backed_off = task.model_copy(update={"next_attempt_at": NOW + timedelta(seconds=60)})
    async with uow_factory() as uow:
        await uow.tasks.update(backed_off, expected_row_version=task.row_version)
        await uow.commit()

    async with uow_factory() as uow:
        assert (
            await uow.tasks.claim_next_assigned(owner="worker-1", now=NOW, lease_seconds=30) is None
        )
        assert (
            await uow.tasks.claim_next_assigned(
                owner="worker-1", now=NOW + timedelta(seconds=61), lease_seconds=30
            )
            is not None
        )


async def test_a_reminder_before_its_due_time_is_not_claimed(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    await seed_task(uow_factory)
    async with uow_factory() as uow:
        await uow.reminders.add(make_reminder())
        await uow.commit()

    async with uow_factory() as uow:
        just_before = DUE - timedelta(microseconds=1)
        assert (
            await uow.reminders.claim_next_due(
                owner="scheduler-1", now=just_before, lease_seconds=30
            )
            is None
        )

    async with uow_factory() as uow:
        assert (
            await uow.reminders.claim_next_due(owner="scheduler-1", now=DUE, lease_seconds=30)
            is not None
        )
        await uow.commit()


async def test_claiming_stops_at_the_attempt_budget(
    uow_factory: SqliteUnitOfWorkFactory,
) -> None:
    task = await seed_task(uow_factory)
    exhausted = task.model_copy(update={"attempt_count": 3, "max_attempts": 3})
    async with uow_factory() as uow:
        await uow.tasks.update(exhausted, expected_row_version=task.row_version)
        await uow.commit()

    async with uow_factory() as uow:
        assert (
            await uow.tasks.claim_next_assigned(owner="worker-1", now=NOW, lease_seconds=30) is None
        )
