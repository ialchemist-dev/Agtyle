"""The database itself must reject illegal writes, not merely the application above it."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import Engine, text

from agtyle.adapters.persistence import migrator
from agtyle.adapters.persistence.models import REQUIRED_TABLES
from agtyle.config import Settings
from agtyle.domain.events import Event, EventType

from .conftest import NOW


def test_migration_up_down_up_is_reversible(settings: Settings, engine: Engine) -> None:
    head = migrator.head_revision(settings)
    assert migrator.current_revision(engine) == head

    migrator.downgrade_to_base(settings)
    assert migrator.current_revision(engine) is None

    migrator.upgrade_to_head(settings)
    assert migrator.current_revision(engine) == head


def test_every_required_table_exists_and_is_strict(engine: Engine) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        ).all()
    definitions = dict(rows)
    for table in REQUIRED_TABLES:
        assert table in definitions, f"missing table {table}"
        assert definitions[table].rstrip().rstrip(";").rstrip().upper().endswith("STRICT")


def test_foreign_keys_are_active_on_every_connection(engine: Engine) -> None:
    for _ in range(2):
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_wal_and_busy_timeout_are_configured(engine: Engine) -> None:
    with engine.connect() as connection:
        assert str(connection.execute(text("PRAGMA journal_mode")).scalar_one()).lower() == "wal"
        assert int(connection.execute(text("PRAGMA busy_timeout")).scalar_one()) >= 5000


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # A non-numeric string cannot be stored in an INTEGER column.
        (
            "INSERT INTO events (id, type, subject, actor, time, data_json, created_at)"
            " VALUES ('evt_1', 'agtyle.task.started.v1', 's', 'a', 'now', '{}', X'00ff')",
            "cannot store blob value in text column",
        ),
        (
            "INSERT INTO tasks (id, intent_id, task_type, status, execution_mode,"
            " assigned_agent_id, objective, payload_json, origin_json, attempt_count,"
            " max_attempts, row_version, created_at, updated_at) VALUES"
            " ('task_x', 'int_x', 't', 'created', 'delegated', 'steward', 'o',"
            " '{}', '{}', 'many', 1, 1, 'now', 'now')",
            "cannot store text value in integer column",
        ),
    ],
)
def test_strict_tables_reject_a_wrong_storage_class(
    engine: Engine, statement: str, expected: str
) -> None:
    with engine.connect() as connection, pytest.raises(Exception) as failure:
        connection.exec_driver_sql(statement)
    assert expected in str(failure.value).lower()


def test_foreign_key_violation_is_rejected(engine: Engine) -> None:
    with engine.connect() as connection, pytest.raises(Exception) as failure:
        connection.exec_driver_sql(
            "INSERT INTO tasks (id, intent_id, task_type, status, execution_mode,"
            " assigned_agent_id, objective, payload_json, origin_json, attempt_count,"
            " max_attempts, row_version, created_at, updated_at) VALUES"
            " ('task_x', 'int_missing', 't', 'created', 'delegated', 'steward', 'o',"
            " '{}', '{}', 0, 1, 1, 'now', 'now')"
        )
    assert "foreign key" in str(failure.value).lower()


def test_invalid_json_is_rejected(engine: Engine) -> None:
    with engine.connect() as connection, pytest.raises(Exception) as failure:
        connection.exec_driver_sql(
            "INSERT INTO events (id, type, subject, actor, time, data_json, created_at)"
            " VALUES ('evt_2', 'agtyle.task.started.v1', 's', 'a', 'now', 'not json', 'now')"
        )
    assert "constraint" in str(failure.value).lower()


def test_enum_check_constraints_reject_unknown_values(engine: Engine) -> None:
    with engine.connect() as connection, pytest.raises(Exception) as failure:
        connection.exec_driver_sql(
            "INSERT INTO reminders (id, user_id, source_task_id, title, scheduled_for_utc,"
            " timezone, status, idempotency_key, created_at, updated_at, row_version)"
            " VALUES ('rem_x', 'u', 'task_x', 't', 'now', 'UTC', 'not_a_status', 'k',"
            " 'now', 'now', 1)"
        )
    assert "constraint" in str(failure.value).lower()


async def test_event_update_and_delete_are_rejected_by_triggers(
    engine: Engine, uow_factory: object
) -> None:
    factory = uow_factory  # type: ignore[assignment]
    async with factory() as uow:  # type: ignore[operator]
        await uow.events.append(
            Event(
                id="evt_00000000-0000-7000-8000-000000000001",
                type=EventType.TASK_STARTED,
                subject="task_1",
                actor="system:kernel",
                time=NOW,
                data={"note": "immutable"},
            )
        )
        await uow.commit()

    with engine.connect() as connection:
        with pytest.raises(Exception) as update_failure:
            connection.exec_driver_sql("UPDATE events SET actor = 'tamper'")
        assert "append-only" in str(update_failure.value)

        with pytest.raises(Exception) as delete_failure:
            connection.exec_driver_sql("DELETE FROM events")
        assert "append-only" in str(delete_failure.value)


def test_event_repository_exposes_no_mutation_methods() -> None:
    from agtyle.adapters.persistence.repositories import SqlEventRepository

    public = {name for name in dir(SqlEventRepository) if not name.startswith("_")}
    assert not public & {"update", "delete", "remove", "purge"}


def test_sqlite_module_is_available_for_direct_introspection() -> None:
    assert sqlite3.sqlite_version_info >= (3, 37), "STRICT tables require SQLite 3.37 or newer"
