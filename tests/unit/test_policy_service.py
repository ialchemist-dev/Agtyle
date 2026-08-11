"""Policy Service mapping: allow, deny, require approval and fail-closed error."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agtyle.adapters.registry import RegisteredCapability, load_registry
from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.application.policy_service import (
    REASON_CAPABILITY_UNREGISTERED,
    REASON_MANIFEST_FORBIDS,
    REASON_SCHEMA_INVALID,
    EvaluateAction,
    PolicyService,
)
from agtyle.application.retry_policy import FailureClass
from agtyle.domain.actions import (
    ActionRequest,
    ActionStatus,
    CedarDecision,
    PolicyOutcome,
    compute_idempotency_key,
    compute_payload_hash,
)
from agtyle.domain.approvals import Approval, ApprovalStatus
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator
from tests.fakes.authorization import ScriptedAuthorization

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
TASK_ID = "task_00000000-0000-7000-8000-000000000001"
ACTION_ID = "act_00000000-0000-7000-8000-000000000001"

PAYLOAD = {
    "title": "submit the report",
    "note": None,
    "scheduled_for_utc": "2026-08-10T21:00:00Z",
    "timezone": "America/Denver",
}

#: The same capability, but marked approval-eligible, so the deny -> require_approval branch
#: can be exercised without inventing a capability that has no committed schema.
APPROVAL_ELIGIBLE_REMINDER = RegisteredCapability(
    name="reminder.create",
    version=1,
    action_schema_id="https://agtyle.local/contracts/schemas/v1/reminder-create.schema.json",
    adapter_name="local_reminders",
    approval_eligible=True,
)


@pytest.fixture(scope="module")
def schemas() -> JsonSchemaRegistry:
    return JsonSchemaRegistry(REPO_ROOT / "contracts")


def make_action(**overrides: object) -> ActionRequest:
    payload = overrides.pop("payload", PAYLOAD)
    capability = str(overrides.pop("capability", "reminder.create"))
    principal = str(overrides.pop("principal_agent_id", "steward"))
    payload_hash = compute_payload_hash(
        principal_agent_id=principal,
        capability=capability,
        resource_type="ReminderCollection",
        resource_id="user_local",
        schema_version=1,
        payload=payload,  # type: ignore[arg-type]
    )
    base: dict[str, object] = {
        "id": ACTION_ID,
        "task_id": TASK_ID,
        "agent_run_id": "run_00000000-0000-7000-8000-000000000001",
        "principal_agent_id": principal,
        "capability": capability,
        "schema_version": 1,
        "resource_type": "ReminderCollection",
        "resource_id": "user_local",
        "payload": payload,
        "payload_hash": payload_hash,
        "idempotency_key": compute_idempotency_key(
            user_id="user_local",
            task_id=TASK_ID,
            capability=capability,
            payload_hash=payload_hash,
        ),
        "status": ActionStatus.PROPOSED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return ActionRequest(**base)  # type: ignore[arg-type]


def build_service(
    schemas: JsonSchemaRegistry,
    authorization: ScriptedAuthorization,
    *,
    capabilities: tuple[RegisteredCapability, ...] | None = None,
) -> PolicyService:
    from agtyle.adapters.registry import BASELINE_CAPABILITIES

    registry = load_registry(
        REPO_ROOT / "agents",
        schemas=schemas,
        capabilities=capabilities if capabilities is not None else BASELINE_CAPABILITIES,
    )
    return PolicyService(
        registry=registry,
        schemas=schemas,
        authorization=authorization,
        clock=FrozenClock(NOW),
        ids=DeterministicIdGenerator(),
    )


def command(**overrides: object) -> EvaluateAction:
    base: dict[str, object] = {
        "action_request": make_action(),
        "origin_user_id": "user_local",
        "has_direct_user_instruction": True,
    }
    base.update(overrides)
    return EvaluateAction(**base)  # type: ignore[arg-type]


async def test_policy_service_maps_allow(schemas: JsonSchemaRegistry) -> None:
    service = build_service(
        schemas, ScriptedAuthorization.allowing("permit-steward-direct-reminder")
    )
    evaluation = await service.evaluate(command())
    assert evaluation.outcome is PolicyOutcome.ALLOW
    assert evaluation.allowed
    assert evaluation.decision.cedar_decision is CedarDecision.ALLOW
    assert evaluation.decision.determining_policy_ids == ["permit-steward-direct-reminder"]


async def test_policy_service_maps_deny_for_a_non_approval_capability(
    schemas: JsonSchemaRegistry,
) -> None:
    service = build_service(schemas, ScriptedAuthorization.denying())
    evaluation = await service.evaluate(command())
    assert evaluation.outcome is PolicyOutcome.DENY
    assert not evaluation.allowed
    assert evaluation.failure_class is FailureClass.AUTHORIZATION_DENIED


async def test_policy_service_maps_deny_to_require_approval_when_eligible(
    schemas: JsonSchemaRegistry,
) -> None:
    service = build_service(
        schemas,
        ScriptedAuthorization.denying(),
        capabilities=(APPROVAL_ELIGIBLE_REMINDER,),
    )
    evaluation = await service.evaluate(command())
    assert evaluation.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert evaluation.decision.reason_code == "approval_required"


async def test_an_approval_eligible_deny_becomes_a_deny_once_an_approval_exists(
    schemas: JsonSchemaRegistry,
) -> None:
    """A held Approval must not be re-requested; Cedar's deny then stands as a deny."""
    service = build_service(
        schemas,
        ScriptedAuthorization.denying(),
        capabilities=(APPROVAL_ELIGIBLE_REMINDER,),
    )
    action = make_action()
    approval = Approval(
        id="apr_00000000-0000-7000-8000-000000000001",
        action_request_id=action.id,
        payload_hash=action.payload_hash,
        principal_agent_id=action.principal_agent_id,
        resource_type=action.resource_type,
        resource_id=action.resource_id,
        approved_by_user_id="user_local",
        status=ApprovalStatus.GRANTED,
        expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
    )
    evaluation = await service.evaluate(command(action_request=action, approval=approval))
    assert evaluation.outcome is PolicyOutcome.DENY


async def test_policy_service_maps_engine_error_to_fail_closed(
    schemas: JsonSchemaRegistry,
) -> None:
    service = build_service(schemas, ScriptedAuthorization.erroring())
    evaluation = await service.evaluate(command())
    assert evaluation.outcome is PolicyOutcome.ERROR
    assert not evaluation.allowed
    assert evaluation.failure_class is FailureClass.AUTHORIZATION_ERROR
    assert evaluation.decision.cedar_decision is CedarDecision.ERROR


async def test_unregistered_capability_is_denied_before_cedar(
    schemas: JsonSchemaRegistry,
) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    evaluation = await service.evaluate(
        command(action_request=make_action(capability="calendar.create_event"))
    )
    assert evaluation.outcome is PolicyOutcome.DENY
    assert evaluation.decision.reason_code == REASON_CAPABILITY_UNREGISTERED
    assert evaluation.decision.cedar_decision is CedarDecision.NOT_EVALUATED
    assert authorization.requests == []


async def test_agent_without_the_manifest_declaration_is_denied_before_cedar(
    schemas: JsonSchemaRegistry,
) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    evaluation = await service.evaluate(
        command(action_request=make_action(principal_agent_id="executive"))
    )
    assert evaluation.outcome is PolicyOutcome.DENY
    assert evaluation.decision.reason_code == REASON_MANIFEST_FORBIDS
    assert authorization.requests == []


async def test_invalid_action_payload_is_denied_before_cedar(
    schemas: JsonSchemaRegistry,
) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    evaluation = await service.evaluate(
        command(action_request=make_action(payload={"title": "", "timezone": "America/Denver"}))
    )
    assert evaluation.outcome is PolicyOutcome.DENY
    assert evaluation.decision.reason_code == REASON_SCHEMA_INVALID
    assert authorization.requests == []


async def test_authorization_context_contains_only_policy_relevant_values(
    schemas: JsonSchemaRegistry,
) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    action = make_action()
    await service.evaluate(command(action_request=action))
    context = authorization.requests[0].context
    assert set(context) == {
        "origin_user_id",
        "has_direct_user_instruction",
        "payload_hash",
        "approval_present",
        "approval_valid",
    }
    assert context["payload_hash"] == action.payload_hash
    assert context["approval_present"] is False


async def test_a_valid_approval_is_reported_to_cedar(schemas: JsonSchemaRegistry) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    action = make_action()
    approval = Approval(
        id="apr_00000000-0000-7000-8000-000000000001",
        action_request_id=action.id,
        payload_hash=action.payload_hash,
        principal_agent_id=action.principal_agent_id,
        resource_type=action.resource_type,
        resource_id=action.resource_id,
        approved_by_user_id="user_local",
        status=ApprovalStatus.GRANTED,
        expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
    )
    await service.evaluate(command(action_request=action, approval=approval))
    context = authorization.requests[0].context
    assert context["approval_present"] is True
    assert context["approval_valid"] is True


async def test_an_expired_approval_is_reported_as_invalid(schemas: JsonSchemaRegistry) -> None:
    authorization = ScriptedAuthorization.allowing()
    service = build_service(schemas, authorization)
    action = make_action()
    approval = Approval(
        id="apr_00000000-0000-7000-8000-000000000001",
        action_request_id=action.id,
        payload_hash=action.payload_hash,
        principal_agent_id=action.principal_agent_id,
        resource_type=action.resource_type,
        resource_id=action.resource_id,
        approved_by_user_id="user_local",
        status=ApprovalStatus.GRANTED,
        expires_at=NOW - timedelta(seconds=1),
        created_at=NOW,
        updated_at=NOW,
    )
    await service.evaluate(command(action_request=action, approval=approval))
    context = authorization.requests[0].context
    assert context["approval_present"] is True
    assert context["approval_valid"] is False


async def test_the_stored_decision_retains_the_normalized_request(
    schemas: JsonSchemaRegistry,
) -> None:
    service = build_service(schemas, ScriptedAuthorization.allowing())
    evaluation = await service.evaluate(command())
    stored = evaluation.decision.request
    assert stored["principal"] == 'Agtyle::Agent::"steward"'
    assert stored["action"] == 'Agtyle::Action::"reminder.create"'
    assert stored["resource"] == 'Agtyle::ReminderCollection::"user_local"'
    assert "payload" not in stored
