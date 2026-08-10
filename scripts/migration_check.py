#!/usr/bin/env python
"""Prove that migrations are reversible and that the resulting schema is correct.

Runs on a throwaway database so it never touches a developer's data:
upgrade to head, assert the revision, downgrade to base, upgrade again, then introspect the
schema for required tables, indexes, unique constraints, STRICT declarations, Event
immutability triggers, foreign-key enforcement and JSON validity.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agtyle.adapters.persistence import migrator  # noqa: E402
from agtyle.adapters.persistence.database import create_database_engine  # noqa: E402
from agtyle.adapters.persistence.models import REQUIRED_TABLES  # noqa: E402
from agtyle.config import Settings  # noqa: E402

REQUIRED_INDEXES = {
    "tasks_claim_idx",
    "reminders_due_idx",
    "notifications_claim_idx",
    "events_subject_time_idx",
    "policy_decisions_action_idx",
}

REQUIRED_UNIQUE = {
    "agent_runs": [("task_id", "attempt")],
    "action_requests": [("idempotency_key",)],
    "action_results": [("action_request_id",)],
    "reminders": [("user_id", "idempotency_key")],
    "notifications": [("delivery_key",)],
    "agents": [("agent_id", "version")],
    "capabilities": [("name", "version")],
}

REQUIRED_TRIGGERS = {"events_reject_update", "events_reject_delete"}


class CheckFailure(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _unique_column_sets(connection: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list('{table}')"):
        if not index[2]:  # not unique
            continue
        columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{index[1]}')"))
        found.add(columns)
    return found


def check_schema(database: Path) -> dict[str, object]:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        require(
            connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
            "foreign key enforcement is not active",
        )

        tables = dict(
            connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        )
        for table in REQUIRED_TABLES:
            require(table in tables, f"missing required table: {table}")
            require(
                tables[table].rstrip().rstrip(";").rstrip().upper().endswith("STRICT"),
                f"table {table} is not declared STRICT",
            )

        indexes = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        missing_indexes = REQUIRED_INDEXES - indexes
        require(not missing_indexes, f"missing required indexes: {sorted(missing_indexes)}")

        for table, expected_sets in REQUIRED_UNIQUE.items():
            found = _unique_column_sets(connection, table)
            for expected in expected_sets:
                require(
                    expected in found,
                    f"{table} is missing UNIQUE{expected}; found {sorted(found)}",
                )

        triggers = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        missing_triggers = REQUIRED_TRIGGERS - triggers
        require(not missing_triggers, f"missing event triggers: {sorted(missing_triggers)}")

        # A foreign-key violation must fail rather than create an orphan row.
        try:
            connection.execute(
                "INSERT INTO tasks (id, intent_id, task_type, status, execution_mode,"
                " assigned_agent_id, objective, payload_json, origin_json, attempt_count,"
                " max_attempts, row_version, created_at, updated_at) VALUES"
                " ('task_x', 'int_missing', 't', 'created', 'delegated', 'steward', 'o',"
                " '{}', '{}', 0, 1, 1, 'now', 'now')"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise CheckFailure("a foreign key violation was accepted")
        connection.rollback()

        # Invalid JSON must be rejected by the CHECK constraint.
        try:
            connection.execute(
                "INSERT INTO events (id, type, subject, actor, time, data_json, created_at)"
                " VALUES ('evt_x', 'agtyle.task.started.v1', 's', 'a', 'now', 'not json', 'now')"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise CheckFailure("invalid JSON was accepted")
        connection.rollback()

        return {
            "tables": len(tables),
            "indexes": len(indexes),
            "triggers": sorted(triggers),
        }
    finally:
        connection.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agtyle-migration-") as workspace:
        directory = Path(workspace)
        database = directory / "migration-check.db"
        settings = Settings(
            env="test",
            data_dir=directory,
            database_url=f"sqlite:///{database}",
            contracts_dir=REPO_ROOT / "contracts",
            agents_dir=REPO_ROOT / "agents",
        )

        head = migrator.upgrade_to_head(settings)
        engine = create_database_engine(settings)
        require(
            migrator.current_revision(engine) == head,
            "current revision does not match head after the first upgrade",
        )
        summary = check_schema(database)

        migrator.downgrade_to_base(settings)
        require(
            migrator.current_revision(engine) is None,
            "downgrade to base left a revision behind",
        )

        migrator.upgrade_to_head(settings)
        require(
            migrator.current_revision(engine) == head,
            "current revision does not match head after the second upgrade",
        )
        check_schema(database)
        engine.dispose()

        print(json.dumps({"status": "passed", "head": head, **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailure as failure:
        print(json.dumps({"status": "failed", "reason": str(failure)}), file=sys.stderr)
        raise SystemExit(1) from failure
