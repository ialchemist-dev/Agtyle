"""A scripted AuthorizationPort for exercising Policy Service mapping only.

This fake exists to test the four-way mapping from a Cedar answer to a business outcome. It is
never used in integration or end-to-end verification, where the real pinned engine runs.
"""

from __future__ import annotations

from agtyle.domain.actions import CedarDecision
from agtyle.ports.authorization import (
    AuthorizationRequest,
    CedarResult,
    PolicyValidationReport,
)


class ScriptedAuthorization:
    adapter_name = "scripted"

    def __init__(self, result: CedarResult, *, valid: bool = True) -> None:
        self._result = result
        self._valid = valid
        self.requests: list[AuthorizationRequest] = []

    async def validate_policy_set(self) -> PolicyValidationReport:
        return PolicyValidationReport(valid=self._valid, engine_version="fake")

    async def authorize(self, request: AuthorizationRequest) -> CedarResult:
        self.requests.append(request)
        return self._result

    @classmethod
    def allowing(cls, *policy_ids: str) -> ScriptedAuthorization:
        return cls(
            CedarResult(
                decision=CedarDecision.ALLOW,
                determining_policy_ids=list(policy_ids),
                reason_code="cedar_allow",
                engine_version="fake",
            )
        )

    @classmethod
    def denying(cls, *policy_ids: str) -> ScriptedAuthorization:
        return cls(
            CedarResult(
                decision=CedarDecision.DENY,
                determining_policy_ids=list(policy_ids),
                reason_code="cedar_deny",
                engine_version="fake",
            )
        )

    @classmethod
    def erroring(cls, reason_code: str = "cedar_timeout") -> ScriptedAuthorization:
        return cls(CedarResult.error(reason_code, detail="scripted failure", engine_version="fake"))
