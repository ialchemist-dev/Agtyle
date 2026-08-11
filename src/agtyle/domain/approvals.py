"""Approval binds an exact ActionRequest. Nothing weaker may authorize an Action."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from agtyle.domain.common import (
    ActionRequestId,
    AgentId,
    ApprovalId,
    DomainModel,
    PayloadHash,
    UserId,
    UtcDatetime,
    require_aware,
)


class ApprovalStatus(StrEnum):
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    REVOKED = "revoked"


class ApprovalInvalidReason(StrEnum):
    NOT_GRANTED = "not_granted"
    EXPIRED = "expired"
    ACTION_MISMATCH = "action_mismatch"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    RESOURCE_MISMATCH = "resource_mismatch"


class Approval(DomainModel):
    """A single-use, expiring grant bound to one exact protected payload."""

    id: ApprovalId
    action_request_id: ActionRequestId
    payload_hash: PayloadHash
    principal_agent_id: AgentId
    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    resource_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    approved_by_user_id: UserId
    status: ApprovalStatus
    expires_at: UtcDatetime
    single_use: bool = True
    consumed_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    row_version: Annotated[int, Field(ge=1)] = 1

    def invalid_reason(
        self,
        *,
        action_request_id: str,
        payload_hash: str,
        principal_agent_id: str,
        resource_type: str,
        resource_id: str,
        now: datetime,
    ) -> ApprovalInvalidReason | None:
        """Return why this Approval cannot authorize the given Action, or ``None`` if valid."""
        if self.status is not ApprovalStatus.GRANTED:
            return ApprovalInvalidReason.NOT_GRANTED
        if require_aware(now, field="now") >= self.expires_at:
            return ApprovalInvalidReason.EXPIRED
        if self.action_request_id != action_request_id:
            return ApprovalInvalidReason.ACTION_MISMATCH
        if self.payload_hash != payload_hash:
            return ApprovalInvalidReason.PAYLOAD_HASH_MISMATCH
        if self.principal_agent_id != principal_agent_id:
            return ApprovalInvalidReason.PRINCIPAL_MISMATCH
        if self.resource_type != resource_type or self.resource_id != resource_id:
            return ApprovalInvalidReason.RESOURCE_MISMATCH
        return None

    def is_valid_for(
        self,
        *,
        action_request_id: str,
        payload_hash: str,
        principal_agent_id: str,
        resource_type: str,
        resource_id: str,
        now: datetime,
    ) -> bool:
        return (
            self.invalid_reason(
                action_request_id=action_request_id,
                payload_hash=payload_hash,
                principal_agent_id=principal_agent_id,
                resource_type=resource_type,
                resource_id=resource_id,
                now=now,
            )
            is None
        )

    def consume(self, *, now: datetime) -> Approval:
        moment = require_aware(now, field="now")
        if self.status is not ApprovalStatus.GRANTED:
            raise ValueError(f"approval in status {self.status.value} cannot be consumed")
        return self.model_copy(
            update={
                "status": ApprovalStatus.CONSUMED if self.single_use else self.status,
                "consumed_at": moment,
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )

    def revoke(self, *, now: datetime) -> Approval:
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "status": ApprovalStatus.REVOKED,
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )

    def expire(self, *, now: datetime) -> Approval:
        moment = require_aware(now, field="now")
        return self.model_copy(
            update={
                "status": ApprovalStatus.EXPIRED,
                "updated_at": moment,
                "row_version": self.row_version + 1,
            }
        )
