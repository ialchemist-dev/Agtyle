"""The physical schema.

Tables are declared once here with SQLAlchemy Core and are the single source of truth for
the initial migration, so the ORM view and the migrated database cannot drift apart.

Every table is a SQLite ``STRICT`` table, so a value of the wrong storage class is rejected by
the database rather than silently coerced. JSON columns are canonical text guarded by
``CHECK(json_valid(...))``; domain payloads are additionally validated against their versioned
JSON Schema before they reach persistence.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable

from agtyle.domain.actions import (
    ActionExecutionStatus,
    ActionStatus,
    CedarDecision,
    PolicyOutcome,
    ReconciliationStatus,
)
from agtyle.domain.agents import AgentRunStatus
from agtyle.domain.approvals import ApprovalStatus
from agtyle.domain.notifications import DeliveryStatus, NotificationKind
from agtyle.domain.reminders import ReminderStatus
from agtyle.domain.tasks import ExecutionMode, TaskStatus

metadata = MetaData()

STRICT: Final[dict[str, Any]] = {"info": {"sqlite_strict": True}}


@compiles(CreateTable, "sqlite")
def _compile_create_table(element: CreateTable, compiler: Any, **kw: Any) -> str:
    """Emit ``STRICT`` for tables that opt in, which SQLAlchemy has no dialect flag for."""
    statement: str = compiler.visit_create_table(element, **kw)
    if element.element.info.get("sqlite_strict"):
        statement = statement.rstrip().rstrip(";").rstrip() + " STRICT"
    return statement


def _enum_check(column: str, values: type) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in values)  # type: ignore[attr-defined]
    return f"{column} IN ({allowed})"


def _json(name: str, *, nullable: bool = False) -> tuple[Column[str], CheckConstraint]:
    return (
        Column(name, Text, nullable=nullable),
        CheckConstraint(f"json_valid({name})", name=f"ck_{name}_valid"),
    )


# --------------------------------------------------------------------------------------
# Core tables
# --------------------------------------------------------------------------------------

intents = Table(
    "intents",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("origin_channel", Text, nullable=False),
    Column("origin_conversation_id", Text, nullable=False),
    Column("interaction_idempotency_key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("original_input", Text, nullable=False),
    Column("interpreted_outcome", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    UniqueConstraint("user_id", "interaction_idempotency_key", name="uq_intents_idempotency"),
    **STRICT,
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("intent_id", Text, ForeignKey("intents.id", ondelete="RESTRICT"), nullable=False),
    Column("task_type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("execution_mode", Text, nullable=False),
    Column("assigned_agent_id", Text, nullable=False),
    Column("objective", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("origin_json", Text, nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    Column("lease_owner", Text, nullable=True),
    Column("lease_expires_at", Text, nullable=True),
    Column("next_attempt_at", Text, nullable=True),
    Column("last_error_code", Text, nullable=True),
    Column("last_error_message", Text, nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    CheckConstraint("json_valid(payload_json)", name="ck_tasks_payload_json_valid"),
    CheckConstraint("json_valid(origin_json)", name="ck_tasks_origin_json_valid"),
    CheckConstraint("attempt_count >= 0", name="ck_tasks_attempt_count"),
    CheckConstraint("max_attempts >= 1", name="ck_tasks_max_attempts"),
    CheckConstraint(_enum_check("status", TaskStatus), name="ck_tasks_status"),
    CheckConstraint(_enum_check("execution_mode", ExecutionMode), name="ck_tasks_execution_mode"),
    Index("tasks_claim_idx", "status", "lease_expires_at", "created_at"),
    Index("tasks_intent_idx", "intent_id"),
    **STRICT,
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("agent_version", Integer, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("context_pack_hash", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("ended_at", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    CheckConstraint("attempt >= 1", name="ck_agent_runs_attempt"),
    CheckConstraint(_enum_check("status", AgentRunStatus), name="ck_agent_runs_status"),
    UniqueConstraint("task_id", "attempt", name="uq_agent_runs_task_attempt"),
    **STRICT,
)

action_requests = Table(
    "action_requests",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("agent_run_id", Text, ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False),
    Column("principal_agent_id", Text, nullable=False),
    Column("capability", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("payload_hash", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("row_version", Integer, nullable=False, server_default="1"),
    CheckConstraint("json_valid(payload_json)", name="ck_action_requests_payload_json_valid"),
    CheckConstraint(_enum_check("status", ActionStatus), name="ck_action_requests_status"),
    Index("action_requests_task_idx", "task_id", "created_at"),
    **STRICT,
)

policy_decisions = Table(
    "policy_decisions",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "action_request_id",
        Text,
        ForeignKey("action_requests.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("decision", Text, nullable=False),
    Column("cedar_decision", Text, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("determining_policy_ids_json", Text, nullable=False),
    Column("request_json", Text, nullable=False),
    Column("cedar_version", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    CheckConstraint(
        "json_valid(determining_policy_ids_json)", name="ck_policy_decisions_policy_ids_valid"
    ),
    CheckConstraint("json_valid(request_json)", name="ck_policy_decisions_request_valid"),
    CheckConstraint(_enum_check("decision", PolicyOutcome), name="ck_policy_decisions_decision"),
    CheckConstraint(
        _enum_check("cedar_decision", CedarDecision), name="ck_policy_decisions_cedar_decision"
    ),
    Index("policy_decisions_action_idx", "action_request_id", "created_at"),
    **STRICT,
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "action_request_id",
        Text,
        ForeignKey("action_requests.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("payload_hash", Text, nullable=False),
    Column("principal_agent_id", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=False),
    Column("approved_by_user_id", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    Column("single_use", Integer, nullable=False),
    Column("consumed_at", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("row_version", Integer, nullable=False, server_default="1"),
    CheckConstraint("single_use IN (0, 1)", name="ck_approvals_single_use"),
    CheckConstraint(_enum_check("status", ApprovalStatus), name="ck_approvals_status"),
    Index("approvals_action_idx", "action_request_id"),
    **STRICT,
)

action_results = Table(
    "action_results",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "action_request_id",
        Text,
        ForeignKey("action_requests.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column("status", Text, nullable=False),
    Column("external_ref", Text, nullable=True),
    Column("result_json", Text, nullable=False),
    Column("reconciliation_status", Text, nullable=False),
    Column("started_at", Text, nullable=False),
    Column("completed_at", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("json_valid(result_json)", name="ck_action_results_result_valid"),
    CheckConstraint(_enum_check("status", ActionExecutionStatus), name="ck_action_results_status"),
    CheckConstraint(
        _enum_check("reconciliation_status", ReconciliationStatus),
        name="ck_action_results_reconciliation",
    ),
    **STRICT,
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("storage_ref", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("metadata_json", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("json_valid(metadata_json)", name="ck_artifacts_metadata_valid"),
    Index("artifacts_task_idx", "task_id"),
    **STRICT,
)

reminders = Table(
    "reminders",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("source_task_id", Text, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
    Column("title", Text, nullable=False),
    Column("note", Text, nullable=True),
    Column("scheduled_for_utc", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("firing_lease_owner", Text, nullable=True),
    Column("firing_lease_expires_at", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("delivered_at", Text, nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    CheckConstraint(_enum_check("status", ReminderStatus), name="ck_reminders_status"),
    UniqueConstraint("user_id", "idempotency_key", name="uq_reminders_idempotency"),
    Index("reminders_due_idx", "status", "scheduled_for_utc"),
    Index("reminders_task_idx", "source_task_id"),
    **STRICT,
)

notifications = Table(
    "notifications",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True),
    Column("reminder_id", Text, ForeignKey("reminders.id", ondelete="RESTRICT"), nullable=True),
    Column("kind", Text, nullable=False),
    Column("destination_json", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("delivery_key", Text, nullable=False, unique=True),
    Column("delivery_status", Text, nullable=False),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    Column("lease_owner", Text, nullable=True),
    Column("lease_expires_at", Text, nullable=True),
    Column("next_attempt_at", Text, nullable=True),
    Column("last_error_code", Text, nullable=True),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("delivered_at", Text, nullable=True),
    Column("row_version", Integer, nullable=False, server_default="1"),
    CheckConstraint("json_valid(destination_json)", name="ck_notifications_destination_valid"),
    CheckConstraint("json_valid(payload_json)", name="ck_notifications_payload_valid"),
    CheckConstraint("attempt_count >= 0", name="ck_notifications_attempt_count"),
    CheckConstraint("max_attempts >= 1", name="ck_notifications_max_attempts"),
    CheckConstraint(
        "task_id IS NOT NULL OR reminder_id IS NOT NULL", name="ck_notifications_subject"
    ),
    CheckConstraint(_enum_check("kind", NotificationKind), name="ck_notifications_kind"),
    CheckConstraint(
        _enum_check("delivery_status", DeliveryStatus), name="ck_notifications_delivery_status"
    ),
    Index("notifications_claim_idx", "delivery_status", "next_attempt_at", "created_at"),
    Index("notifications_task_idx", "task_id"),
    Index("notifications_reminder_idx", "reminder_id"),
    **STRICT,
)

events = Table(
    "events",
    metadata,
    Column("id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("time", Text, nullable=False),
    Column("data_json", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("json_valid(data_json)", name="ck_events_data_valid"),
    Index("events_subject_time_idx", "subject", "time"),
    **STRICT,
)

agents = Table(
    "agents",
    metadata,
    Column("agent_id", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("manifest_hash", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("registered_at", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("enabled IN (0, 1)", name="ck_agents_enabled"),
    UniqueConstraint("agent_id", "version", name="uq_agents_id_version"),
    **STRICT,
)

capabilities = Table(
    "capabilities",
    metadata,
    Column("name", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("action_schema_id", Text, nullable=False),
    Column("adapter_name", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("registered_at", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    CheckConstraint("enabled IN (0, 1)", name="ck_capabilities_enabled"),
    UniqueConstraint("name", "version", name="uq_capabilities_name_version"),
    **STRICT,
)

# --------------------------------------------------------------------------------------
# Event immutability
# --------------------------------------------------------------------------------------

EVENT_IMMUTABILITY_TRIGGERS: Final[tuple[str, ...]] = (
    """
    CREATE TRIGGER events_reject_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only: UPDATE is not permitted');
    END
    """,
    """
    CREATE TRIGGER events_reject_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only: DELETE is not permitted');
    END
    """,
)

DROP_EVENT_IMMUTABILITY_TRIGGERS: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS events_reject_update",
    "DROP TRIGGER IF EXISTS events_reject_delete",
)

REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "intents",
    "tasks",
    "agent_runs",
    "action_requests",
    "policy_decisions",
    "approvals",
    "action_results",
    "artifacts",
    "reminders",
    "notifications",
    "events",
    "agents",
    "capabilities",
)
