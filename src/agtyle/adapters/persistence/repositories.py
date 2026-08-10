"""Repository implementations.

Every method accepts and returns domain models. Rows are translated at this boundary so no
SQLAlchemy construct escapes into application code, and no domain invariant is bypassed by
writing a raw row.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Connection, Row, Select, and_, func, or_, select, update
from sqlalchemy import Table as SaTable

from agtyle.adapters.persistence import models
from agtyle.domain.actions import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    PolicyDecision,
)
from agtyle.domain.agents import AgentRun
from agtyle.domain.approvals import Approval
from agtyle.domain.artifacts import Artifact
from agtyle.domain.common import (
    ErrorCode,
    from_storage,
    to_storage,
)
from agtyle.domain.events import Event
from agtyle.domain.intents import Intent
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
)
from agtyle.domain.reminders import Reminder, ReminderStatus
from agtyle.domain.tasks import Task, TaskOrigin, TaskStatus
from agtyle.ports.repositories import ConcurrentUpdateError

T = TypeVar("T")


def _dt(value: datetime | None) -> str | None:
    return to_storage(value) if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return from_storage(value) if value is not None else None


def _dumps(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str) -> Any:
    return json.loads(raw)


class _Base:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def _fetch_one(self, statement: Select[Any]) -> Row[Any] | None:
        return self._connection.execute(statement).one_or_none()

    def _fetch_all(self, statement: Select[Any]) -> list[Row[Any]]:
        return list(self._connection.execute(statement).all())

    def _insert(self, table: SaTable, values: dict[str, Any]) -> None:
        self._connection.execute(table.insert().values(**values))

    def _optimistic_update(
        self,
        table: SaTable,
        *,
        primary_key: dict[str, Any],
        expected_row_version: int,
        values: dict[str, Any],
    ) -> None:
        """Update one row only if its version still matches, so a lost race is explicit."""
        conditions = [table.c[column] == value for column, value in primary_key.items()]
        conditions.append(table.c.row_version == expected_row_version)
        result = self._connection.execute(update(table).where(and_(*conditions)).values(**values))
        if result.rowcount != 1:
            raise ConcurrentUpdateError(
                f"{table.name} row {primary_key} was modified by another process"
            )


# --------------------------------------------------------------------------------------
# Intents
# --------------------------------------------------------------------------------------


def _intent_from_row(row: Row[Any]) -> Intent:
    return Intent(
        id=row.id,
        user_id=row.user_id,
        origin_channel=row.origin_channel,
        origin_conversation_id=row.origin_conversation_id,
        interaction_idempotency_key=row.interaction_idempotency_key,
        request_hash=row.request_hash,
        original_input=row.original_input,
        interpreted_outcome=row.interpreted_outcome,
        created_at=from_storage(row.created_at),
    )


class SqlIntentRepository(_Base):
    async def add(self, intent: Intent) -> None:
        self._insert(
            models.intents,
            {
                "id": intent.id,
                "user_id": intent.user_id,
                "origin_channel": intent.origin_channel,
                "origin_conversation_id": intent.origin_conversation_id,
                "interaction_idempotency_key": intent.interaction_idempotency_key,
                "request_hash": intent.request_hash,
                "original_input": intent.original_input,
                "interpreted_outcome": intent.interpreted_outcome,
                "created_at": to_storage(intent.created_at),
            },
        )

    async def get(self, intent_id: str) -> Intent | None:
        row = self._fetch_one(select(models.intents).where(models.intents.c.id == intent_id))
        return _intent_from_row(row) if row else None

    async def find_by_idempotency_key(self, *, user_id: str, key: str) -> Intent | None:
        row = self._fetch_one(
            select(models.intents).where(
                models.intents.c.user_id == user_id,
                models.intents.c.interaction_idempotency_key == key,
            )
        )
        return _intent_from_row(row) if row else None

    async def count_all(self) -> int:
        return int(
            self._connection.execute(select(func.count()).select_from(models.intents)).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


def _task_from_row(row: Row[Any]) -> Task:
    return Task(
        id=row.id,
        intent_id=row.intent_id,
        task_type=row.task_type,
        status=TaskStatus(row.status),
        execution_mode=row.execution_mode,
        assigned_agent_id=row.assigned_agent_id,
        objective=row.objective,
        payload=_loads(row.payload_json),
        origin=TaskOrigin(**_loads(row.origin_json)),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=_parse_dt(row.lease_expires_at),
        next_attempt_at=_parse_dt(row.next_attempt_at),
        last_error_code=ErrorCode(row.last_error_code) if row.last_error_code else None,
        last_error_message=row.last_error_message,
        row_version=row.row_version,
        created_at=from_storage(row.created_at),
        updated_at=from_storage(row.updated_at),
    )


def _task_values(task: Task) -> dict[str, Any]:
    return {
        "task_type": task.task_type,
        "status": task.status.value,
        "execution_mode": task.execution_mode.value,
        "assigned_agent_id": task.assigned_agent_id,
        "objective": task.objective,
        "payload_json": _dumps(task.payload),
        "origin_json": _dumps(task.origin.model_dump()),
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "lease_owner": task.lease_owner,
        "lease_expires_at": _dt(task.lease_expires_at),
        "next_attempt_at": _dt(task.next_attempt_at),
        "last_error_code": task.last_error_code.value if task.last_error_code else None,
        "last_error_message": task.last_error_message,
        "row_version": task.row_version,
        "updated_at": to_storage(task.updated_at),
    }


class SqlTaskRepository(_Base):
    async def add(self, task: Task) -> None:
        self._insert(
            models.tasks,
            {
                "id": task.id,
                "intent_id": task.intent_id,
                "created_at": to_storage(task.created_at),
                **_task_values(task),
            },
        )

    async def get(self, task_id: str) -> Task | None:
        row = self._fetch_one(select(models.tasks).where(models.tasks.c.id == task_id))
        return _task_from_row(row) if row else None

    async def list_by_intent(self, intent_id: str) -> list[Task]:
        rows = self._fetch_all(
            select(models.tasks)
            .where(models.tasks.c.intent_id == intent_id)
            .order_by(models.tasks.c.created_at, models.tasks.c.id)
        )
        return [_task_from_row(row) for row in rows]

    async def update(self, task: Task, *, expected_row_version: int) -> None:
        self._optimistic_update(
            models.tasks,
            primary_key={"id": task.id},
            expected_row_version=expected_row_version,
            values=_task_values(task),
        )

    async def claim_next_assigned(
        self, *, owner: str, now: datetime, lease_seconds: int
    ) -> Task | None:
        """Claim the oldest claimable Task with a conditional update.

        SQLite has no ``SKIP LOCKED``. Correctness comes from the conditional ``UPDATE``:
        the winner is whichever process changes exactly one row whose version is unchanged.
        """
        stamp = to_storage(now)
        candidates = self._fetch_all(
            select(models.tasks)
            .where(
                models.tasks.c.status == TaskStatus.ASSIGNED.value,
                or_(
                    models.tasks.c.next_attempt_at.is_(None),
                    models.tasks.c.next_attempt_at <= stamp,
                ),
                models.tasks.c.attempt_count < models.tasks.c.max_attempts,
            )
            .order_by(models.tasks.c.created_at, models.tasks.c.id)
            .limit(20)
        )
        for row in candidates:
            task = _task_from_row(row)
            claimed = task.start(lease_owner=owner, now=now, lease_seconds=lease_seconds)
            result = self._connection.execute(
                update(models.tasks)
                .where(
                    models.tasks.c.id == task.id,
                    models.tasks.c.status == TaskStatus.ASSIGNED.value,
                    models.tasks.c.row_version == task.row_version,
                )
                .values(**_task_values(claimed))
            )
            if result.rowcount == 1:
                return claimed
        return None

    async def list_expired_running(self, *, now: datetime, limit: int = 100) -> list[Task]:
        stamp = to_storage(now)
        rows = self._fetch_all(
            select(models.tasks)
            .where(
                models.tasks.c.status == TaskStatus.RUNNING.value,
                or_(
                    models.tasks.c.lease_expires_at.is_(None),
                    models.tasks.c.lease_expires_at <= stamp,
                ),
            )
            .order_by(models.tasks.c.created_at, models.tasks.c.id)
            .limit(limit)
        )
        return [_task_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(select(func.count()).select_from(models.tasks)).scalar_one()
        )


# --------------------------------------------------------------------------------------
# AgentRuns
# --------------------------------------------------------------------------------------


def _agent_run_from_row(row: Row[Any]) -> AgentRun:
    return AgentRun(
        id=row.id,
        task_id=row.task_id,
        agent_id=row.agent_id,
        agent_version=row.agent_version,
        attempt=row.attempt,
        status=row.status,
        context_pack_hash=row.context_pack_hash,
        started_at=from_storage(row.started_at),
        ended_at=_parse_dt(row.ended_at),
        error_code=ErrorCode(row.error_code) if row.error_code else None,
        error_message=row.error_message,
    )


class SqlAgentRunRepository(_Base):
    async def add(self, run: AgentRun) -> None:
        self._insert(
            models.agent_runs,
            {
                "id": run.id,
                "task_id": run.task_id,
                "agent_id": run.agent_id,
                "agent_version": run.agent_version,
                "attempt": run.attempt,
                "status": run.status.value,
                "context_pack_hash": run.context_pack_hash,
                "started_at": to_storage(run.started_at),
                "ended_at": _dt(run.ended_at),
                "error_code": run.error_code.value if run.error_code else None,
                "error_message": run.error_message,
                "created_at": to_storage(run.started_at),
            },
        )

    async def get(self, run_id: str) -> AgentRun | None:
        row = self._fetch_one(select(models.agent_runs).where(models.agent_runs.c.id == run_id))
        return _agent_run_from_row(row) if row else None

    async def update(self, run: AgentRun) -> None:
        result = self._connection.execute(
            update(models.agent_runs)
            .where(models.agent_runs.c.id == run.id)
            .values(
                status=run.status.value,
                ended_at=_dt(run.ended_at),
                error_code=run.error_code.value if run.error_code else None,
                error_message=run.error_message,
            )
        )
        if result.rowcount != 1:
            raise ConcurrentUpdateError(f"agent_run {run.id} not found")

    async def list_for_task(self, task_id: str) -> list[AgentRun]:
        rows = self._fetch_all(
            select(models.agent_runs)
            .where(models.agent_runs.c.task_id == task_id)
            .order_by(models.agent_runs.c.attempt)
        )
        return [_agent_run_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.agent_runs)
            ).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Actions, decisions, results and artifacts
# --------------------------------------------------------------------------------------


def _action_from_row(row: Row[Any]) -> ActionRequest:
    return ActionRequest(
        id=row.id,
        task_id=row.task_id,
        agent_run_id=row.agent_run_id,
        principal_agent_id=row.principal_agent_id,
        capability=row.capability,
        schema_version=row.schema_version,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        payload=_loads(row.payload_json),
        payload_hash=row.payload_hash,
        idempotency_key=row.idempotency_key,
        status=ActionStatus(row.status),
        created_at=from_storage(row.created_at),
        updated_at=from_storage(row.updated_at),
        row_version=row.row_version,
    )


def _decision_from_row(row: Row[Any]) -> PolicyDecision:
    return PolicyDecision(
        id=row.id,
        action_request_id=row.action_request_id,
        decision=row.decision,
        cedar_decision=row.cedar_decision,
        reason_code=row.reason_code,
        determining_policy_ids=_loads(row.determining_policy_ids_json),
        request=_loads(row.request_json),
        cedar_version=row.cedar_version,
        created_at=from_storage(row.created_at),
    )


def _result_from_row(row: Row[Any]) -> ActionResult:
    return ActionResult(
        id=row.id,
        action_request_id=row.action_request_id,
        status=row.status,
        external_ref=row.external_ref,
        result=_loads(row.result_json),
        reconciliation_status=row.reconciliation_status,
        started_at=from_storage(row.started_at),
        completed_at=from_storage(row.completed_at),
        created_at=from_storage(row.created_at),
    )


def _artifact_from_row(row: Row[Any]) -> Artifact:
    return Artifact(
        id=row.id,
        task_id=row.task_id,
        kind=row.kind,
        media_type=row.media_type,
        storage_ref=row.storage_ref,
        content_hash=row.content_hash,
        metadata=_loads(row.metadata_json),
        created_at=from_storage(row.created_at),
    )


class SqlActionRepository(_Base):
    async def add(self, request: ActionRequest) -> None:
        self._insert(
            models.action_requests,
            {
                "id": request.id,
                "task_id": request.task_id,
                "agent_run_id": request.agent_run_id,
                "principal_agent_id": request.principal_agent_id,
                "capability": request.capability,
                "schema_version": request.schema_version,
                "resource_type": request.resource_type,
                "resource_id": request.resource_id,
                "payload_json": _dumps(request.payload),
                "payload_hash": request.payload_hash,
                "idempotency_key": request.idempotency_key,
                "status": request.status.value,
                "created_at": to_storage(request.created_at),
                "updated_at": to_storage(request.updated_at),
                "row_version": request.row_version,
            },
        )

    async def get(self, action_request_id: str) -> ActionRequest | None:
        row = self._fetch_one(
            select(models.action_requests).where(models.action_requests.c.id == action_request_id)
        )
        return _action_from_row(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> ActionRequest | None:
        row = self._fetch_one(
            select(models.action_requests).where(models.action_requests.c.idempotency_key == key)
        )
        return _action_from_row(row) if row else None

    async def update(self, request: ActionRequest, *, expected_row_version: int) -> None:
        self._optimistic_update(
            models.action_requests,
            primary_key={"id": request.id},
            expected_row_version=expected_row_version,
            values={
                "status": request.status.value,
                "payload_json": _dumps(request.payload),
                "payload_hash": request.payload_hash,
                "updated_at": to_storage(request.updated_at),
                "row_version": request.row_version,
            },
        )

    async def list_for_task(self, task_id: str) -> list[ActionRequest]:
        rows = self._fetch_all(
            select(models.action_requests)
            .where(models.action_requests.c.task_id == task_id)
            .order_by(models.action_requests.c.created_at, models.action_requests.c.id)
        )
        return [_action_from_row(row) for row in rows]

    async def add_policy_decision(self, decision: PolicyDecision) -> None:
        self._insert(
            models.policy_decisions,
            {
                "id": decision.id,
                "action_request_id": decision.action_request_id,
                "decision": decision.decision.value,
                "cedar_decision": decision.cedar_decision.value,
                "reason_code": decision.reason_code,
                "determining_policy_ids_json": _dumps(decision.determining_policy_ids),
                "request_json": _dumps(decision.request),
                "cedar_version": decision.cedar_version,
                "created_at": to_storage(decision.created_at),
            },
        )

    async def list_policy_decisions(self, action_request_id: str) -> list[PolicyDecision]:
        rows = self._fetch_all(
            select(models.policy_decisions)
            .where(models.policy_decisions.c.action_request_id == action_request_id)
            .order_by(models.policy_decisions.c.created_at, models.policy_decisions.c.id)
        )
        return [_decision_from_row(row) for row in rows]

    async def add_result(self, result: ActionResult) -> None:
        self._insert(
            models.action_results,
            {
                "id": result.id,
                "action_request_id": result.action_request_id,
                "status": result.status.value,
                "external_ref": result.external_ref,
                "result_json": _dumps(result.result),
                "reconciliation_status": result.reconciliation_status.value,
                "started_at": to_storage(result.started_at),
                "completed_at": to_storage(result.completed_at),
                "created_at": to_storage(result.created_at),
            },
        )

    async def get_result(self, action_request_id: str) -> ActionResult | None:
        row = self._fetch_one(
            select(models.action_results).where(
                models.action_results.c.action_request_id == action_request_id
            )
        )
        return _result_from_row(row) if row else None

    async def add_artifact(self, artifact: Artifact) -> None:
        self._insert(
            models.artifacts,
            {
                "id": artifact.id,
                "task_id": artifact.task_id,
                "kind": artifact.kind.value,
                "media_type": artifact.media_type,
                "storage_ref": artifact.storage_ref,
                "content_hash": artifact.content_hash,
                "metadata_json": _dumps(artifact.metadata),
                "created_at": to_storage(artifact.created_at),
            },
        )

    async def list_artifacts(self, task_id: str) -> list[Artifact]:
        rows = self._fetch_all(
            select(models.artifacts)
            .where(models.artifacts.c.task_id == task_id)
            .order_by(models.artifacts.c.created_at, models.artifacts.c.id)
        )
        return [_artifact_from_row(row) for row in rows]

    async def count_requests(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.action_requests)
            ).scalar_one()
        )

    async def count_decisions(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.policy_decisions)
            ).scalar_one()
        )

    async def count_results(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.action_results)
            ).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------


def _approval_from_row(row: Row[Any]) -> Approval:
    return Approval(
        id=row.id,
        action_request_id=row.action_request_id,
        payload_hash=row.payload_hash,
        principal_agent_id=row.principal_agent_id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        approved_by_user_id=row.approved_by_user_id,
        status=row.status,
        expires_at=from_storage(row.expires_at),
        single_use=bool(row.single_use),
        consumed_at=_parse_dt(row.consumed_at),
        created_at=from_storage(row.created_at),
        updated_at=from_storage(row.updated_at),
        row_version=row.row_version,
    )


class SqlApprovalRepository(_Base):
    async def add(self, approval: Approval) -> None:
        self._insert(
            models.approvals,
            {
                "id": approval.id,
                "action_request_id": approval.action_request_id,
                "payload_hash": approval.payload_hash,
                "principal_agent_id": approval.principal_agent_id,
                "resource_type": approval.resource_type,
                "resource_id": approval.resource_id,
                "approved_by_user_id": approval.approved_by_user_id,
                "status": approval.status.value,
                "expires_at": to_storage(approval.expires_at),
                "single_use": int(approval.single_use),
                "consumed_at": _dt(approval.consumed_at),
                "created_at": to_storage(approval.created_at),
                "updated_at": to_storage(approval.updated_at),
                "row_version": approval.row_version,
            },
        )

    async def get(self, approval_id: str) -> Approval | None:
        row = self._fetch_one(select(models.approvals).where(models.approvals.c.id == approval_id))
        return _approval_from_row(row) if row else None

    async def update(self, approval: Approval, *, expected_row_version: int) -> None:
        self._optimistic_update(
            models.approvals,
            primary_key={"id": approval.id},
            expected_row_version=expected_row_version,
            values={
                "status": approval.status.value,
                "consumed_at": _dt(approval.consumed_at),
                "updated_at": to_storage(approval.updated_at),
                "row_version": approval.row_version,
            },
        )

    async def list_for_action(self, action_request_id: str) -> list[Approval]:
        rows = self._fetch_all(
            select(models.approvals)
            .where(models.approvals.c.action_request_id == action_request_id)
            .order_by(models.approvals.c.created_at, models.approvals.c.id)
        )
        return [_approval_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.approvals)
            ).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------------------


def _reminder_from_row(row: Row[Any]) -> Reminder:
    return Reminder(
        id=row.id,
        user_id=row.user_id,
        source_task_id=row.source_task_id,
        title=row.title,
        note=row.note,
        scheduled_for_utc=from_storage(row.scheduled_for_utc),
        timezone=row.timezone,
        status=ReminderStatus(row.status),
        idempotency_key=row.idempotency_key,
        firing_lease_owner=row.firing_lease_owner,
        firing_lease_expires_at=_parse_dt(row.firing_lease_expires_at),
        created_at=from_storage(row.created_at),
        updated_at=from_storage(row.updated_at),
        delivered_at=_parse_dt(row.delivered_at),
        row_version=row.row_version,
    )


def _reminder_values(reminder: Reminder) -> dict[str, Any]:
    return {
        "title": reminder.title,
        "note": reminder.note,
        "scheduled_for_utc": to_storage(reminder.scheduled_for_utc),
        "timezone": reminder.timezone,
        "status": reminder.status.value,
        "firing_lease_owner": reminder.firing_lease_owner,
        "firing_lease_expires_at": _dt(reminder.firing_lease_expires_at),
        "updated_at": to_storage(reminder.updated_at),
        "delivered_at": _dt(reminder.delivered_at),
        "row_version": reminder.row_version,
    }


class SqlReminderRepository(_Base):
    async def add(self, reminder: Reminder) -> None:
        self._insert(
            models.reminders,
            {
                "id": reminder.id,
                "user_id": reminder.user_id,
                "source_task_id": reminder.source_task_id,
                "idempotency_key": reminder.idempotency_key,
                "created_at": to_storage(reminder.created_at),
                **_reminder_values(reminder),
            },
        )

    async def get(self, reminder_id: str) -> Reminder | None:
        row = self._fetch_one(select(models.reminders).where(models.reminders.c.id == reminder_id))
        return _reminder_from_row(row) if row else None

    async def get_by_idempotency_key(self, *, user_id: str, key: str) -> Reminder | None:
        row = self._fetch_one(
            select(models.reminders).where(
                models.reminders.c.user_id == user_id,
                models.reminders.c.idempotency_key == key,
            )
        )
        return _reminder_from_row(row) if row else None

    async def update(self, reminder: Reminder, *, expected_row_version: int) -> None:
        self._optimistic_update(
            models.reminders,
            primary_key={"id": reminder.id},
            expected_row_version=expected_row_version,
            values=_reminder_values(reminder),
        )

    async def claim_next_due(
        self, *, owner: str, now: datetime, lease_seconds: int
    ) -> Reminder | None:
        """Move exactly one due Reminder from `scheduled` to `firing`.

        Two Schedulers running concurrently can both read the same candidate, but only one
        conditional update changes a row, so only one due Notification is ever created.
        """
        from datetime import timedelta

        stamp = to_storage(now)
        candidates = self._fetch_all(
            select(models.reminders)
            .where(
                models.reminders.c.status == ReminderStatus.SCHEDULED.value,
                models.reminders.c.scheduled_for_utc <= stamp,
            )
            .order_by(models.reminders.c.scheduled_for_utc, models.reminders.c.id)
            .limit(20)
        )
        for row in candidates:
            reminder = _reminder_from_row(row)
            firing = reminder.begin_firing(
                now=now,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            result = self._connection.execute(
                update(models.reminders)
                .where(
                    models.reminders.c.id == reminder.id,
                    models.reminders.c.status == ReminderStatus.SCHEDULED.value,
                    models.reminders.c.row_version == reminder.row_version,
                )
                .values(**_reminder_values(firing))
            )
            if result.rowcount == 1:
                return firing
        return None

    async def list_for_task(self, task_id: str) -> list[Reminder]:
        rows = self._fetch_all(
            select(models.reminders)
            .where(models.reminders.c.source_task_id == task_id)
            .order_by(models.reminders.c.created_at, models.reminders.c.id)
        )
        return [_reminder_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.reminders)
            ).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------------------


def _notification_from_row(row: Row[Any]) -> Notification:
    return Notification(
        id=row.id,
        task_id=row.task_id,
        reminder_id=row.reminder_id,
        kind=row.kind,
        destination=NotificationDestination(**_loads(row.destination_json)),
        payload=_loads(row.payload_json),
        delivery_key=row.delivery_key,
        delivery_status=DeliveryStatus(row.delivery_status),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=_parse_dt(row.lease_expires_at),
        next_attempt_at=_parse_dt(row.next_attempt_at),
        last_error_code=ErrorCode(row.last_error_code) if row.last_error_code else None,
        created_at=from_storage(row.created_at),
        updated_at=from_storage(row.updated_at),
        delivered_at=_parse_dt(row.delivered_at),
        row_version=row.row_version,
    )


def _notification_values(notification: Notification) -> dict[str, Any]:
    return {
        "kind": notification.kind.value,
        "destination_json": _dumps(notification.destination.model_dump()),
        "payload_json": _dumps(notification.payload),
        "delivery_status": notification.delivery_status.value,
        "attempt_count": notification.attempt_count,
        "max_attempts": notification.max_attempts,
        "lease_owner": notification.lease_owner,
        "lease_expires_at": _dt(notification.lease_expires_at),
        "next_attempt_at": _dt(notification.next_attempt_at),
        "last_error_code": (
            notification.last_error_code.value if notification.last_error_code else None
        ),
        "updated_at": to_storage(notification.updated_at),
        "delivered_at": _dt(notification.delivered_at),
        "row_version": notification.row_version,
    }


class SqlNotificationRepository(_Base):
    async def add(self, notification: Notification) -> None:
        self._insert(
            models.notifications,
            {
                "id": notification.id,
                "task_id": notification.task_id,
                "reminder_id": notification.reminder_id,
                "delivery_key": notification.delivery_key,
                "created_at": to_storage(notification.created_at),
                **_notification_values(notification),
            },
        )

    async def get(self, notification_id: str) -> Notification | None:
        row = self._fetch_one(
            select(models.notifications).where(models.notifications.c.id == notification_id)
        )
        return _notification_from_row(row) if row else None

    async def get_by_delivery_key(self, delivery_key: str) -> Notification | None:
        row = self._fetch_one(
            select(models.notifications).where(models.notifications.c.delivery_key == delivery_key)
        )
        return _notification_from_row(row) if row else None

    async def update(self, notification: Notification, *, expected_row_version: int) -> None:
        self._optimistic_update(
            models.notifications,
            primary_key={"id": notification.id},
            expected_row_version=expected_row_version,
            values=_notification_values(notification),
        )

    async def claim_next_pending(
        self, *, owner: str, now: datetime, lease_seconds: int
    ) -> Notification | None:
        stamp = to_storage(now)
        candidates = self._fetch_all(
            select(models.notifications)
            .where(
                models.notifications.c.delivery_status == DeliveryStatus.PENDING.value,
                or_(
                    models.notifications.c.next_attempt_at.is_(None),
                    models.notifications.c.next_attempt_at <= stamp,
                ),
                models.notifications.c.attempt_count < models.notifications.c.max_attempts,
            )
            .order_by(models.notifications.c.created_at, models.notifications.c.id)
            .limit(20)
        )
        for row in candidates:
            notification = _notification_from_row(row)
            claimed = notification.begin_delivery(owner=owner, now=now, lease_seconds=lease_seconds)
            result = self._connection.execute(
                update(models.notifications)
                .where(
                    models.notifications.c.id == notification.id,
                    models.notifications.c.delivery_status == DeliveryStatus.PENDING.value,
                    models.notifications.c.row_version == notification.row_version,
                )
                .values(**_notification_values(claimed))
            )
            if result.rowcount == 1:
                return claimed
        return None

    async def list_for_task(self, task_id: str) -> list[Notification]:
        rows = self._fetch_all(
            select(models.notifications)
            .where(models.notifications.c.task_id == task_id)
            .order_by(models.notifications.c.created_at, models.notifications.c.id)
        )
        return [_notification_from_row(row) for row in rows]

    async def list_for_reminder(self, reminder_id: str) -> list[Notification]:
        rows = self._fetch_all(
            select(models.notifications)
            .where(models.notifications.c.reminder_id == reminder_id)
            .order_by(models.notifications.c.created_at, models.notifications.c.id)
        )
        return [_notification_from_row(row) for row in rows]

    async def list_expired_delivering(
        self, *, now: datetime, limit: int = 100
    ) -> list[Notification]:
        stamp = to_storage(now)
        rows = self._fetch_all(
            select(models.notifications)
            .where(
                models.notifications.c.delivery_status == DeliveryStatus.DELIVERING.value,
                or_(
                    models.notifications.c.lease_expires_at.is_(None),
                    models.notifications.c.lease_expires_at <= stamp,
                ),
            )
            .order_by(models.notifications.c.created_at, models.notifications.c.id)
            .limit(limit)
        )
        return [_notification_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(
                select(func.count()).select_from(models.notifications)
            ).scalar_one()
        )


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def _event_from_row(row: Row[Any]) -> Event:
    return Event(
        id=row.id,
        type=row.type,
        subject=row.subject,
        actor=row.actor,
        time=from_storage(row.time),
        data=_loads(row.data_json),
    )


class SqlEventRepository(_Base):
    """Append and query only.

    There is deliberately no update or delete method. Database triggers reject those
    statements as well, so a mistake in this layer cannot quietly rewrite history.
    """

    async def append(self, event: Event) -> None:
        self._insert(
            models.events,
            {
                "id": event.id,
                "type": event.type.value,
                "subject": event.subject,
                "actor": event.actor,
                "time": to_storage(event.time),
                "data_json": _dumps(event.data),
                "created_at": to_storage(event.time),
            },
        )

    async def list_by_subject(self, subject: str) -> list[Event]:
        rows = self._fetch_all(
            select(models.events)
            .where(models.events.c.subject == subject)
            .order_by(models.events.c.time, models.events.c.id)
        )
        return [_event_from_row(row) for row in rows]

    async def list_by_subjects(self, subjects: list[str]) -> list[Event]:
        if not subjects:
            return []
        rows = self._fetch_all(
            select(models.events)
            .where(models.events.c.subject.in_(subjects))
            .order_by(models.events.c.time, models.events.c.id)
        )
        return [_event_from_row(row) for row in rows]

    async def count_all(self) -> int:
        return int(
            self._connection.execute(select(func.count()).select_from(models.events)).scalar_one()
        )
