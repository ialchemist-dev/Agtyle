"""Separate operating-system processes must share one SQLite database safely (§8).

The concurrency tests elsewhere use threads, which share a process and a SQLite library
instance. This suite spawns real subprocesses, because cross-process locking is a different
mechanism: `BEGIN IMMEDIATE` plus the busy timeout is what makes two independent processes
queue rather than corrupt or double-claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agtyle.bootstrap import build_container, migrate
from agtyle.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def shared(settings: Settings) -> Iterator[Settings]:
    migrate(settings)
    container = build_container(settings, configure_logs=False)
    try:
        import asyncio

        asyncio.run(container.registry_sync.sync())
    finally:
        container.dispose()
    yield settings


def role_environment(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AGTYLE_ENV": "test",
            "AGTYLE_DATA_DIR": str(settings.data_dir),
            "AGTYLE_DATABASE_URL": settings.database_url,
            "AGTYLE_NOTIFICATION_ADAPTER": "console",
            "AGTYLE_LOG_LEVEL": "ERROR",
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    return env


def run_role(settings: Settings, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agtyle.cli", *arguments],
        cwd=REPO_ROOT,
        env=role_environment(settings),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


#: Subprocesses run on the real clock, so the reminder they create must be genuinely future.
FUTURE_INPUT = "Remind me to verify multi-process sharing at 2099-01-01T00:00:00Z"


def claimed_task_id(completed: subprocess.CompletedProcess[str]) -> str | None:
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    report = document.get("report")
    return str(report["task_id"]) if report else None


async def test_two_worker_processes_claim_one_task(shared: Settings) -> None:
    """Two real processes racing for the same Task: exactly one may claim it."""
    from .conftest import interaction

    container = build_container(shared, configure_logs=False)
    try:
        await container.interaction_service.handle(interaction(text=FUTURE_INPUT))
    finally:
        container.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_role(shared, "worker", "--once"), range(2)))

    for completed in results:
        assert completed.returncode == 0, completed.stderr[-800:]

    claimed = [task_id for task_id in map(claimed_task_id, results) if task_id]
    assert len(claimed) == 1, f"expected exactly one process to claim, saw {claimed}"

    verifier = build_container(shared, configure_logs=False)
    try:
        async with verifier.uow_factory() as uow:
            tasks = await uow.tasks.count_all()
            runs = await uow.agent_runs.count_all()
            reminders = await uow.reminders.count_all()
    finally:
        verifier.dispose()
    assert (tasks, runs, reminders) == (1, 1, 1)


async def test_two_scheduler_processes_fire_a_reminder_once(shared: Settings) -> None:
    """Two real Scheduler processes racing a due Reminder must create one due Notification."""
    from .conftest import interaction

    container = build_container(shared, configure_logs=False)
    try:
        await container.interaction_service.handle(interaction(text=FUTURE_INPUT))
    finally:
        container.dispose()

    assert run_role(shared, "worker", "--once").returncode == 0

    # Move the stored due time into the past instead of sleeping: the subprocesses run on the
    # real clock, and the property under test is cross-process exclusion, not scheduling delay.
    mover = build_container(shared, configure_logs=False)
    try:
        async with mover.uow_factory() as uow:
            stored = (await uow.reminders.list_for_task(await _only_task_id(uow)))[0]
            await uow.reminders.update(
                stored.model_copy(
                    update={
                        "scheduled_for_utc": datetime(2020, 1, 1, tzinfo=UTC),
                        "row_version": stored.row_version + 1,
                    }
                ),
                expected_row_version=stored.row_version,
            )
            await uow.commit()
    finally:
        mover.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_role(shared, "scheduler", "--once"), range(2)))
    for completed in results:
        assert completed.returncode == 0, completed.stderr[-800:]

    fired = [
        completed
        for completed in results
        if '"fired": {' in completed.stdout or '"fired": null' not in completed.stdout
    ]
    assert len(fired) == 1, "exactly one scheduler process may fire a reminder"

    verifier = build_container(shared, configure_logs=False)
    try:
        async with verifier.uow_factory() as uow:
            reminder = (await uow.reminders.list_for_task(await _only_task_id(uow)))[0]
            due = await uow.notifications.list_for_reminder(reminder.id)
    finally:
        verifier.dispose()
    assert len(due) == 1, f"two schedulers created {len(due)} due notifications"


async def _only_task_id(uow: object) -> str:
    """The single Task in a fixture database, fetched without assuming an identifier."""
    tasks = await uow.tasks.list_expired_running(  # type: ignore[attr-defined]
        now=datetime(2100, 1, 1, tzinfo=UTC), limit=10
    )
    if tasks:
        return str(tasks[0].id)
    intents = await uow.intents.find_by_idempotency_key(  # type: ignore[attr-defined]
        user_id="user_local", key="e2e-reminder-001"
    )
    assert intents is not None
    linked = await uow.tasks.list_by_intent(intents.id)  # type: ignore[attr-defined]
    return str(linked[0].id)


async def test_separate_processes_agree_on_the_same_database(shared: Settings) -> None:
    """A row written by one process must be visible to the next, with WAL and foreign keys on."""
    from .conftest import interaction

    container = build_container(shared, configure_logs=False)
    try:
        result = await container.interaction_service.handle(interaction())
    finally:
        container.dispose()
    assert result.task_receipt is not None

    completed = run_role(shared, "task", "show", result.task_receipt.task_id)
    assert completed.returncode == 0, completed.stderr[-800:]
    document = json.loads(completed.stdout)
    assert document["task"]["id"] == result.task_receipt.task_id
    assert document["task"]["status"] == "assigned"
