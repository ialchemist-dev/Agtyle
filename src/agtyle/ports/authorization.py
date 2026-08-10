"""Authorization port. Cedar is mandatory on the Action path and fails closed."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agtyle.domain.actions import CedarDecision
from agtyle.domain.common import DomainModel, JsonMapping


class AuthorizationRequest(DomainModel):
    """The normalized authorization question, stored verbatim on the PolicyDecision."""

    principal_type: str
    principal_id: str
    action_id: str
    resource_type: str
    resource_id: str
    context: JsonMapping

    def to_document(self) -> JsonMapping:
        return {
            "action": f'Agtyle::Action::"{self.action_id}"',
            "context": self.context,
            "principal": f'Agtyle::{self.principal_type}::"{self.principal_id}"',
            "resource": f'Agtyle::{self.resource_type}::"{self.resource_id}"',
        }


class CedarResult(DomainModel):
    """The engine's answer plus the diagnostics needed to explain it later."""

    decision: CedarDecision
    determining_policy_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    engine_version: str | None = None
    reason_code: str = "evaluated"

    @classmethod
    def error(
        cls, reason_code: str, *, detail: str, engine_version: str | None = None
    ) -> CedarResult:
        return cls(
            decision=CedarDecision.ERROR,
            reason_code=reason_code,
            errors=[detail],
            engine_version=engine_version,
        )


class PolicyValidationReport(DomainModel):
    """Result of validating the policy set against the schema at startup and in CI."""

    valid: bool
    engine_version: str | None = None
    policy_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class AuthorizationPort(Protocol):
    async def validate_policy_set(self) -> PolicyValidationReport:
        """Parse and validate schema and policies. Invalid configuration blocks execution."""
        ...

    async def authorize(self, request: AuthorizationRequest) -> CedarResult:
        """Evaluate one request. Any failure must map to ``CedarDecision.ERROR``."""
        ...
