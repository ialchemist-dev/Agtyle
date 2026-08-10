"""ActionRequest, canonical payload hashing, idempotency keys, PolicyDecision, ActionResult."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from agtyle.domain.common import (
    ActionRequestId,
    ActionResultId,
    AgentId,
    AgentRunId,
    CapabilityName,
    DomainModel,
    IdempotencyKey,
    JsonMapping,
    PayloadHash,
    PolicyDecisionId,
    TaskId,
    UtcDatetime,
    hash_document,
    sha256_hex,
)

SCHEMA_VERSION: Final[Literal[1]] = 1


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    AUTHORIZED = "authorized"
    WAITING_APPROVAL = "waiting_approval"
    DENIED = "denied"
    EXECUTING = "executing"
    APPLIED = "applied"
    FAILED = "failed"


class PolicyOutcome(StrEnum):
    """The three business outcomes plus the fail-closed error state."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    ERROR = "error"


class CedarDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NOT_EVALUATED = "not_evaluated"
    ERROR = "error"


class ReconciliationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    RECONCILED = "reconciled"
    PENDING = "pending"


class ActionExecutionStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    FAILED = "failed"


def protected_document(
    *,
    principal_agent_id: str,
    capability: str,
    resource_type: str,
    resource_id: str,
    schema_version: int,
    payload: JsonMapping,
) -> JsonMapping:
    """The exact document covered by ``payload_hash``.

    Database identifiers, timestamps, status and the hash itself are deliberately
    excluded so that a retry of the same logical Action hashes identically.
    """
    return {
        "capability": capability,
        "payload": payload,
        "principal_agent_id": principal_agent_id,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "schema_version": schema_version,
    }


def compute_payload_hash(
    *,
    principal_agent_id: str,
    capability: str,
    resource_type: str,
    resource_id: str,
    schema_version: int,
    payload: JsonMapping,
) -> str:
    return hash_document(
        protected_document(
            principal_agent_id=principal_agent_id,
            capability=capability,
            resource_type=resource_type,
            resource_id=resource_id,
            schema_version=schema_version,
            payload=payload,
        )
    )


def compute_idempotency_key(
    *,
    user_id: str,
    task_id: str,
    capability: str,
    payload_hash: str,
) -> str:
    """``sha256(user_id | task_id | capability | payload_hash)``, computed by the kernel."""
    return sha256_hex("|".join((user_id, task_id, capability, payload_hash)))


class ActionRequest(DomainModel):
    """A typed, hashed, authorizable capability invocation owned by the kernel."""

    id: ActionRequestId
    task_id: TaskId
    agent_run_id: AgentRunId
    principal_agent_id: AgentId
    capability: CapabilityName
    schema_version: Literal[1]
    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    resource_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    payload: JsonMapping
    payload_hash: PayloadHash
    idempotency_key: IdempotencyKey
    status: ActionStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    row_version: Annotated[int, Field(ge=1)] = 1

    def protected_document(self) -> JsonMapping:
        return protected_document(
            principal_agent_id=self.principal_agent_id,
            capability=self.capability,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            schema_version=self.schema_version,
            payload=self.payload,
        )

    def recompute_payload_hash(self) -> str:
        return hash_document(self.protected_document())

    def hash_matches(self) -> bool:
        return self.recompute_payload_hash() == self.payload_hash

    def with_status(self, status: ActionStatus, *, now: UtcDatetime) -> ActionRequest:
        return self.model_copy(
            update={"status": status, "updated_at": now, "row_version": self.row_version + 1}
        )


class PolicyDecision(DomainModel):
    """The stored, replayable record of one authorization evaluation."""

    id: PolicyDecisionId
    action_request_id: ActionRequestId
    decision: PolicyOutcome
    cedar_decision: CedarDecision
    reason_code: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    determining_policy_ids: list[str] = Field(default_factory=list)
    request: JsonMapping
    cedar_version: str | None = None
    created_at: UtcDatetime


class ActionResult(DomainModel):
    """The durable outcome of a capability invocation. Exactly one per ActionRequest."""

    id: ActionResultId
    action_request_id: ActionRequestId
    status: ActionExecutionStatus
    external_ref: str | None = None
    result: JsonMapping = Field(default_factory=dict)
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.NOT_REQUIRED
    started_at: UtcDatetime
    completed_at: UtcDatetime
    created_at: UtcDatetime
