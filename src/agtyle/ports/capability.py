"""Capability port: the only place a consequential effect may happen."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agtyle.domain.actions import (
    ActionExecutionStatus,
    ActionRequest,
    ReconciliationStatus,
)
from agtyle.domain.common import DomainModel, JsonMapping


class ActionExecutionResult(DomainModel):
    """What the adapter did, expressed so the kernel can persist an ActionResult."""

    status: ActionExecutionStatus
    external_ref: str | None = None
    result: JsonMapping = Field(default_factory=dict)
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.NOT_REQUIRED


class ReconciliationResult(DomainModel):
    """Whether a prior attempt already produced the effect for an idempotency key."""

    found: bool
    external_ref: str | None = None
    result: JsonMapping = Field(default_factory=dict)


@runtime_checkable
class CapabilityPort(Protocol):
    capability_name: str
    schema_version: int

    async def execute(self, request: ActionRequest) -> ActionExecutionResult:
        """Apply the effect exactly once for the request's idempotency key."""
        ...

    async def reconcile(self, idempotency_key: str) -> ReconciliationResult:
        """Report whether the effect already exists, so a retry cannot duplicate it."""
        ...
