"""Drift detection.

If a Pydantic model changes shape, the committed JSON Schema and the published API contract must
change with it — deliberately, in the same commit. These tests fail on silent divergence rather
than letting a consumer discover it.
"""

from __future__ import annotations

import json
from typing import Any

from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.domain.actions import ActionRequest, ActionStatus, PolicyOutcome
from agtyle.domain.agents import ActionRequestProposal
from agtyle.domain.approvals import ApprovalStatus
from agtyle.domain.common import ErrorCode
from agtyle.domain.notifications import DeliveryStatus, NotificationKind
from agtyle.domain.reminders import ReminderCreatePayload, ReminderStatus
from agtyle.domain.tasks import ExecutionMode, TaskStatus

REMINDER_SCHEMA_ID = "https://agtyle.local/contracts/schemas/v1/reminder-create.schema.json"
ACTION_SCHEMA_ID = "https://agtyle.local/contracts/schemas/v1/action-request.schema.json"


def _properties(document: dict[str, Any]) -> set[str]:
    return set(document.get("properties", {}))


def test_reminder_payload_fields_match_the_committed_schema(
    registry: JsonSchemaRegistry,
) -> None:
    document = registry.document(REMINDER_SCHEMA_ID)
    assert _properties(document) == set(ReminderCreatePayload.model_fields)


def test_reminder_payload_required_fields_match(registry: JsonSchemaRegistry) -> None:
    document = registry.document(REMINDER_SCHEMA_ID)
    required_in_model = {
        name for name, field in ReminderCreatePayload.model_fields.items() if field.is_required()
    }
    assert set(document["required"]) == required_in_model


def test_action_proposal_fields_match_the_committed_envelope(
    registry: JsonSchemaRegistry,
) -> None:
    document = registry.document(ACTION_SCHEMA_ID)
    assert _properties(document) == set(ActionRequestProposal.model_fields)
    assert document["additionalProperties"] is False


def test_the_proposal_envelope_excludes_every_kernel_owned_field() -> None:
    """An Agent must never be able to name a control value the kernel owns."""
    kernel_owned = set(ActionRequest.model_fields) - set(ActionRequestProposal.model_fields)
    assert {
        "id",
        "task_id",
        "agent_run_id",
        "principal_agent_id",
        "payload_hash",
        "idempotency_key",
        "status",
        "created_at",
        "updated_at",
        "row_version",
    } <= kernel_owned


def test_the_error_taxonomy_is_stable() -> None:
    """These codes are a public contract. Changing one is a breaking change for consumers."""
    assert {code.name: code.value for code in ErrorCode} == {
        "INVALID_REQUEST": "AGT-INPUT-001",
        "CLARIFICATION_REQUIRED": "AGT-INPUT-002",
        "INTERACTION_IDEMPOTENCY_CONFLICT": "AGT-INPUT-003",
        "ILLEGAL_TRANSITION": "AGT-TASK-001",
        "LEASE_LOST": "AGT-TASK-002",
        "RETRY_EXHAUSTED": "AGT-TASK-003",
        "INVALID_AGENT_OUTPUT": "AGT-AGENT-001",
        "UNSUPPORTED_ASSIGNMENT": "AGT-AGENT-002",
        "AGENT_RUNTIME_TIMEOUT": "AGT-AGENT-003",
        "POLICY_DENIED": "AGT-POLICY-001",
        "APPROVAL_REQUIRED": "AGT-POLICY-002",
        "POLICY_ENGINE_ERROR": "AGT-POLICY-003",
        "ACTION_SCHEMA_INVALID": "AGT-ACTION-001",
        "ACTION_IDEMPOTENCY_CONFLICT": "AGT-ACTION-002",
        "CAPABILITY_TRANSIENT_FAILURE": "AGT-CAP-001",
        "CAPABILITY_PERMANENT_FAILURE": "AGT-CAP-002",
        "NOTIFICATION_DELIVERY_FAILED": "AGT-NOTIFY-001",
        "CONFIGURATION_INVALID": "AGT-SYSTEM-001",
    }


def test_domain_enum_values_are_stable() -> None:
    """These strings are persisted in a STRICT database with CHECK constraints."""
    assert [item.value for item in TaskStatus] == [
        "created",
        "assigned",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "cancelled",
    ]
    assert [item.value for item in ExecutionMode] == [
        "interactive",
        "delegated",
        "approval_gated",
    ]
    assert [item.value for item in ReminderStatus] == [
        "scheduled",
        "firing",
        "delivered",
        "cancelled",
        "failed",
    ]
    assert [item.value for item in NotificationKind] == [
        "task_completed",
        "task_failed",
        "approval_required",
        "reminder_due",
    ]
    assert [item.value for item in DeliveryStatus] == [
        "pending",
        "delivering",
        "delivered",
        "failed",
    ]
    assert [item.value for item in PolicyOutcome] == [
        "allow",
        "require_approval",
        "deny",
        "error",
    ]
    assert {item.value for item in ApprovalStatus} == {
        "granted",
        "rejected",
        "expired",
        "consumed",
        "revoked",
    }
    assert "applied" in {item.value for item in ActionStatus}


def test_every_committed_schema_declares_an_id_and_version(
    registry: JsonSchemaRegistry,
) -> None:
    for schema_id in registry.schema_ids:
        document = registry.document(schema_id)
        assert document["$id"] == schema_id
        assert document["x-agtyle-schema-version"] == 1
        assert document["$schema"].startswith("https://json-schema.org/draft/2020-12")


def test_every_schema_file_is_valid_json_and_stably_formatted(registry: JsonSchemaRegistry) -> None:
    from .conftest import CONTRACTS

    for path in sorted((CONTRACTS / "schemas").rglob("*.schema.json")):
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        assert isinstance(document, dict)
        assert text.endswith("\n"), f"{path.name} must end with a newline"
