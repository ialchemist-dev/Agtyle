"""Approval: bind a human decision to one exact ActionRequest.

An Approval is not a flag on a Task. It names the precise payload hash, principal, action and
resource it authorizes, it expires, and (by default) it is single use. Consuming it and moving
the ActionRequest back to an executable state happen in one transaction, so a consumed Approval
can never authorize a second Action.
"""

from __future__ import annotations

from datetime import timedelta

from agtyle.domain.actions import ActionRequest, ActionStatus
from agtyle.domain.approvals import Approval, ApprovalStatus
from agtyle.domain.common import (
    ApprovalId,
    DomainModel,
    IdPrefix,
    InvalidRequestError,
    UserId,
)
from agtyle.domain.events import Event, EventType, user_actor
from agtyle.domain.tasks import TaskStatus
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.repositories import UnitOfWorkFactory

DEFAULT_EXPIRY = timedelta(minutes=15)


class GrantApproval(DomainModel):
    action_request_id: str
    approved_by_user_id: UserId
    expires_in_seconds: int = int(DEFAULT_EXPIRY.total_seconds())
    single_use: bool = True


class RejectApproval(DomainModel):
    action_request_id: str
    rejected_by_user_id: UserId


class ConsumeApproval(DomainModel):
    approval_id: ApprovalId


class ApprovalService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: ClockPort,
        ids: IdGeneratorPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def grant(self, command: GrantApproval) -> Approval:
        """Create an Approval bound to the exact protected payload of one ActionRequest."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            action = await uow.actions.get(command.action_request_id)
            if action is None:
                await uow.rollback()
                raise InvalidRequestError(f"no action request {command.action_request_id}")
            if action.status is not ActionStatus.WAITING_APPROVAL:
                await uow.rollback()
                raise InvalidRequestError(
                    f"action {action.id} is {action.status.value}, not waiting for approval"
                )

            approval = Approval(
                id=self._ids.new_id(IdPrefix.APPROVAL),
                action_request_id=action.id,
                payload_hash=action.payload_hash,
                principal_agent_id=action.principal_agent_id,
                resource_type=action.resource_type,
                resource_id=action.resource_id,
                approved_by_user_id=command.approved_by_user_id,
                status=ApprovalStatus.GRANTED,
                expires_at=now + timedelta(seconds=command.expires_in_seconds),
                single_use=command.single_use,
                created_at=now,
                updated_at=now,
            )
            await uow.approvals.add(approval)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.APPROVAL_GRANTED,
                    subject=action.task_id,
                    actor=user_actor(command.approved_by_user_id),
                    time=now,
                    data={
                        "approval_id": approval.id,
                        "action_request_id": action.id,
                        "payload_hash": action.payload_hash,
                        "expires_at": approval.expires_at.isoformat(),
                    },
                )
            )
            await uow.commit()
        return approval

    async def reject(self, command: RejectApproval) -> Approval:
        """Record a refusal and terminate the Task. A rejected Action never runs."""
        now = self._clock.now()
        async with self._uow_factory() as uow:
            action = await uow.actions.get(command.action_request_id)
            if action is None:
                await uow.rollback()
                raise InvalidRequestError(f"no action request {command.action_request_id}")

            approval = Approval(
                id=self._ids.new_id(IdPrefix.APPROVAL),
                action_request_id=action.id,
                payload_hash=action.payload_hash,
                principal_agent_id=action.principal_agent_id,
                resource_type=action.resource_type,
                resource_id=action.resource_id,
                approved_by_user_id=command.rejected_by_user_id,
                status=ApprovalStatus.REJECTED,
                expires_at=now,
                single_use=True,
                created_at=now,
                updated_at=now,
            )
            await uow.approvals.add(approval)
            await uow.actions.update(
                action.with_status(ActionStatus.DENIED, now=now),
                expected_row_version=action.row_version,
            )

            task = await uow.tasks.get(action.task_id)
            if task is not None and task.status is TaskStatus.WAITING_APPROVAL:
                await uow.tasks.update(task.cancel(now=now), expected_row_version=task.row_version)
            await uow.commit()
        return approval

    async def consume(self, command: ConsumeApproval) -> tuple[Approval, ActionRequest]:
        """Consume the Approval and return its ActionRequest to an executable state, atomically.

        If these were separate transactions, a crash between them would either burn an Approval
        that authorized nothing, or leave a consumed Approval able to authorize twice.
        """
        now = self._clock.now()
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get(command.approval_id)
            if approval is None:
                await uow.rollback()
                raise InvalidRequestError(f"no approval {command.approval_id}")

            action = await uow.actions.get(approval.action_request_id)
            if action is None:
                await uow.rollback()
                raise InvalidRequestError(f"no action request {approval.action_request_id}")

            reason = approval.invalid_reason(
                action_request_id=action.id,
                payload_hash=action.payload_hash,
                principal_agent_id=action.principal_agent_id,
                resource_type=action.resource_type,
                resource_id=action.resource_id,
                now=now,
            )
            if reason is not None:
                await uow.rollback()
                raise InvalidRequestError(f"approval cannot authorize this action: {reason.value}")

            consumed = approval.consume(now=now)
            await uow.approvals.update(consumed, expected_row_version=approval.row_version)
            executable = action.with_status(ActionStatus.VALIDATED, now=now)
            await uow.actions.update(executable, expected_row_version=action.row_version)

            task = await uow.tasks.get(action.task_id)
            if task is not None and task.status is TaskStatus.WAITING_APPROVAL:
                await uow.tasks.update(
                    task.resume(lease_owner="approval", now=now, lease_seconds=1),
                    expected_row_version=task.row_version,
                )
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.APPROVAL_CONSUMED,
                    subject=action.task_id,
                    actor=user_actor(approval.approved_by_user_id),
                    time=now,
                    data={"approval_id": approval.id, "action_request_id": action.id},
                )
            )
            await uow.commit()
        return consumed, executable
