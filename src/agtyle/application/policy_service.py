"""Policy Service: the single gate between a proposed Action and a Capability Adapter.

Every Action passes through here, in this order:

1. is the capability registered at this schema version?
2. does the principal Agent's manifest declare that it may request it?
3. does the payload satisfy the committed versioned schema?
4. what does the real Cedar engine decide?

Only an ``ALLOW`` permits execution. A ``deny`` is a policy decision; an ``error`` is a
fail-closed infrastructure outcome. They are stored distinctly because they mean different
things to an operator: one is the system working, the other is the system unable to decide.
"""

from __future__ import annotations

from agtyle.application.retry_policy import FailureClass
from agtyle.domain.actions import (
    ActionRequest,
    CedarDecision,
    PolicyDecision,
    PolicyOutcome,
)
from agtyle.domain.approvals import Approval
from agtyle.domain.common import DomainModel, IdPrefix, JsonMapping
from agtyle.ports.authorization import AuthorizationPort, AuthorizationRequest, CedarResult
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.registry import AgentRegistryPort
from agtyle.ports.schema_registry import ActionSchemaPort

REASON_CAPABILITY_UNREGISTERED = "capability_unregistered"
REASON_MANIFEST_FORBIDS = "manifest_does_not_declare_capability"
REASON_SCHEMA_INVALID = "action_schema_invalid"
REASON_APPROVAL_REQUIRED = "approval_required"


class EvaluateAction(DomainModel):
    """Everything the Policy Service needs, and nothing it does not."""

    action_request: ActionRequest
    origin_user_id: str
    has_direct_user_instruction: bool
    approval: Approval | None = None


class PolicyEvaluation(DomainModel):
    """The outcome plus the durable record that explains it.

    The decision is returned rather than written so the caller can persist it inside the same
    transaction as the ActionRequest update.
    """

    outcome: PolicyOutcome
    decision: PolicyDecision

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    @property
    def failure_class(self) -> FailureClass:
        """How a Worker should treat this outcome if it is not an allow."""
        if self.outcome is PolicyOutcome.ERROR:
            return FailureClass.AUTHORIZATION_ERROR
        return FailureClass.AUTHORIZATION_DENIED


class PolicyService:
    def __init__(
        self,
        *,
        registry: AgentRegistryPort,
        schemas: ActionSchemaPort,
        authorization: AuthorizationPort,
        clock: ClockPort,
        ids: IdGeneratorPort,
    ) -> None:
        self._registry = registry
        self._schemas = schemas
        self._authorization = authorization
        self._clock = clock
        self._ids = ids

    async def evaluate(self, command: EvaluateAction) -> PolicyEvaluation:
        request = command.action_request
        authorization_request = self.build_authorization_request(command)

        capability = self._registry.capability(request.capability, request.schema_version)
        if capability is None:
            return self._decide(
                request,
                authorization_request,
                outcome=PolicyOutcome.DENY,
                cedar=CedarDecision.NOT_EVALUATED,
                reason=REASON_CAPABILITY_UNREGISTERED,
            )

        if not self._registry.agent_may_request(request.principal_agent_id, request.capability):
            # The manifest is a declaration of intent, checked before the engine is consulted.
            return self._decide(
                request,
                authorization_request,
                outcome=PolicyOutcome.DENY,
                cedar=CedarDecision.NOT_EVALUATED,
                reason=REASON_MANIFEST_FORBIDS,
            )

        schema_result = self._schemas.validate_payload(
            capability=request.capability,
            schema_version=request.schema_version,
            payload=request.payload,
        )
        if not schema_result.valid:
            return self._decide(
                request,
                authorization_request,
                outcome=PolicyOutcome.DENY,
                cedar=CedarDecision.NOT_EVALUATED,
                reason=REASON_SCHEMA_INVALID,
            )

        cedar: CedarResult = await self._authorization.authorize(authorization_request)

        if cedar.decision is CedarDecision.ALLOW:
            outcome = PolicyOutcome.ALLOW
            reason = cedar.reason_code
        elif cedar.decision is CedarDecision.DENY:
            # Only an approval-eligible capability turns a deny into an approval prompt.
            # reminder.create is reversible and directly user-requested, so a deny means deny.
            if capability.approval_eligible and not self._approval_is_valid(command):
                outcome = PolicyOutcome.REQUIRE_APPROVAL
                reason = REASON_APPROVAL_REQUIRED
            else:
                outcome = PolicyOutcome.DENY
                reason = cedar.reason_code
        else:
            outcome = PolicyOutcome.ERROR
            reason = cedar.reason_code

        return self._decide(
            request,
            authorization_request,
            outcome=outcome,
            cedar=cedar.decision,
            reason=reason,
            policy_ids=cedar.determining_policy_ids,
            engine_version=cedar.engine_version,
        )

    def build_authorization_request(self, command: EvaluateAction) -> AuthorizationRequest:
        """Normalize the authorization question. Only policy-relevant values are included."""
        request = command.action_request
        approval_present = command.approval is not None
        return AuthorizationRequest(
            principal_type="Agent",
            principal_id=request.principal_agent_id,
            action_id=request.capability,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            context={
                "origin_user_id": command.origin_user_id,
                "has_direct_user_instruction": command.has_direct_user_instruction,
                "payload_hash": request.payload_hash,
                "approval_present": approval_present,
                "approval_valid": self._approval_is_valid(command),
            },
        )

    def _approval_is_valid(self, command: EvaluateAction) -> bool:
        """An Approval only counts when it binds this exact Action and has not expired."""
        approval = command.approval
        if approval is None:
            return False
        request = command.action_request
        return approval.is_valid_for(
            action_request_id=request.id,
            payload_hash=request.payload_hash,
            principal_agent_id=request.principal_agent_id,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            now=self._clock.now(),
        )

    def _decide(
        self,
        request: ActionRequest,
        authorization_request: AuthorizationRequest,
        *,
        outcome: PolicyOutcome,
        cedar: CedarDecision,
        reason: str,
        policy_ids: list[str] | None = None,
        engine_version: str | None = None,
    ) -> PolicyEvaluation:
        stored_request: JsonMapping = authorization_request.to_document()
        return PolicyEvaluation(
            outcome=outcome,
            decision=PolicyDecision(
                id=self._ids.new_id(IdPrefix.POLICY_DECISION),
                action_request_id=request.id,
                decision=outcome,
                cedar_decision=cedar,
                reason_code=reason,
                determining_policy_ids=policy_ids or [],
                request=stored_request,
                cedar_version=engine_version,
                created_at=self._clock.now(),
            ),
        )
