"""Agent registry contracts and the untrusted structured output Agents may return."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agtyle.domain.common import (
    AgentId,
    AgentRunId,
    CapabilityName,
    DomainModel,
    ErrorCode,
    JsonMapping,
    TaskId,
    UtcDatetime,
)
from agtyle.domain.tasks import ExecutionMode


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class EscalationPolicy(StrEnum):
    NEVER = "never"
    WHEN_OUTCOME_CHANGES = "when_outcome_changes"
    ALWAYS = "always"


class AuthorityLevel(StrEnum):
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"


class CapabilityRequest(DomainModel):
    requested: list[CapabilityName] = Field(default_factory=list)


class AgentAuthority(DomainModel):
    external_publish: AuthorityLevel = AuthorityLevel.FORBIDDEN


class AgentEscalation(DomainModel):
    ambiguity: EscalationPolicy = EscalationPolicy.WHEN_OUTCOME_CHANGES


class AgentManifest(DomainModel):
    """The machine-verifiable contract that declares what an Agent may do."""

    id: AgentId
    version: Annotated[int, Field(ge=1)]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    accepts: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]]
    produces: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]]
    capabilities: CapabilityRequest = Field(default_factory=CapabilityRequest)
    context_scopes: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list
    )
    authority: AgentAuthority = Field(default_factory=AgentAuthority)
    escalation: AgentEscalation = Field(default_factory=AgentEscalation)

    def may_request(self, capability: str) -> bool:
        return capability in self.capabilities.requested

    def accepts_type(self, assignment_type: str) -> bool:
        return assignment_type in self.accepts


class AgentRun(DomainModel):
    """One attempt at executing a Task with a specific Agent."""

    id: AgentRunId
    task_id: TaskId
    agent_id: AgentId
    agent_version: Annotated[int, Field(ge=1)]
    attempt: Annotated[int, Field(ge=1)]
    status: AgentRunStatus
    context_pack_hash: str
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None

    def succeed(self, *, now: UtcDatetime) -> AgentRun:
        return self.model_copy(update={"status": AgentRunStatus.SUCCEEDED, "ended_at": now})

    def fail(self, *, now: UtcDatetime, error_code: ErrorCode, error_message: str) -> AgentRun:
        return self.model_copy(
            update={
                "status": AgentRunStatus.FAILED,
                "ended_at": now,
                "error_code": error_code,
                "error_message": error_message,
            }
        )

    def abandon(self, *, now: UtcDatetime) -> AgentRun:
        """Mark a run whose Worker lost its lease; a new attempt supersedes it."""
        return self.model_copy(
            update={
                "status": AgentRunStatus.ABANDONED,
                "ended_at": now,
                "error_code": ErrorCode.LEASE_LOST,
                "error_message": "worker lease expired before the run finished",
            }
        )


# --------------------------------------------------------------------------------------
# ContextPack: the only context an Agent receives
# --------------------------------------------------------------------------------------


class TrustLabel(StrEnum):
    USER_DIRECT = "user_direct"
    SYSTEM = "system"
    UNTRUSTED_EXTERNAL = "untrusted_external"


class ContextTask(DomainModel):
    id: TaskId
    task_type: str
    objective: str
    payload: JsonMapping = Field(default_factory=dict)


class ContextPack(DomainModel):
    """Constrained context. Contains no credentials, no full history, no knowledge dump."""

    task: ContextTask
    references: list[str] = Field(default_factory=list)
    user_preferences: JsonMapping = Field(default_factory=dict)
    authority_budget: list[CapabilityName] = Field(default_factory=list)
    trust_labels: dict[str, TrustLabel] = Field(default_factory=dict)


class AgentAssignment(DomainModel):
    """What the kernel asks an Agent to do."""

    assignment_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    agent_id: AgentId
    task_id: TaskId | None = None
    agent_run_id: AgentRunId | None = None
    input_text: str | None = None
    payload: JsonMapping = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Agent output: untrusted structured data
# --------------------------------------------------------------------------------------


class UntrustedModel(BaseModel):
    """Base for Agent output. ``extra='forbid'`` makes unexpected fields a hard failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskProposal(UntrustedModel):
    kind: Literal["task_proposal"] = "task_proposal"
    task_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    assigned_agent_id: AgentId
    execution_mode: ExecutionMode
    objective: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    payload: JsonMapping = Field(default_factory=dict)


class DirectResponse(UntrustedModel):
    kind: Literal["direct_response"] = "direct_response"
    message: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ClarificationResponse(UntrustedModel):
    kind: Literal["clarification_required"] = "clarification_required"
    message: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    missing: list[str] = Field(default_factory=list)


class ActionRequestProposal(UntrustedModel):
    """An Agent proposes a capability invocation. The kernel owns every control value."""

    kind: Literal["action_request"] = "action_request"
    capability: CapabilityName
    schema_version: Literal[1]
    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    resource_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    payload: JsonMapping


ExecutiveOutput = TaskProposal | DirectResponse | ClarificationResponse
SpecialistOutput = ActionRequestProposal | DirectResponse | ClarificationResponse
AgentOutput = TaskProposal | DirectResponse | ClarificationResponse | ActionRequestProposal
