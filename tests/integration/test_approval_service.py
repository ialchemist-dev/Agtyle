"""Approval binds one exact ActionRequest, expires, and is consumed atomically."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agtyle.application.approval_service import (
    ApprovalService,
    ConsumeApproval,
    GrantApproval,
    RejectApproval,
)
from agtyle.bootstrap import Container
from agtyle.domain.actions import (
    ActionRequest,
    ActionStatus,
    compute_idempotency_key,
    compute_payload_hash,
)
from agtyle.domain.agents import AgentRun, AgentRunStatus
from agtyle.domain.approvals import ApprovalStatus
from agtyle.domain.common import InvalidRequestError
from agtyle.domain.tasks import TaskStatus

from .conftest import NOW, interaction

PAYLOAD = {
    "title": "submit the report",
    "note": None,
    "scheduled_for_utc": "2026-08-09T22:05:00Z",
    "timezone": "America/Denver",
}


@pytest.fixture
def approvals(container: Container) -> ApprovalService:
    return ApprovalService(
        uow_factory=container.uow_factory, clock=container.clock, ids=container.ids
    )


async def _waiting_action(container: Container) -> ActionRequest:
    """Create a Task parked on an ActionRequest that is waiting for approval."""
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    task_id = result.task_receipt.task_id

    payload_hash = compute_payload_hash(
        principal_agent_id="steward",
        capability="reminder.create",
        resource_type="ReminderCollection",
        resource_id="user_local",
        schema_version=1,
        payload=PAYLOAD,
    )
    action = ActionRequest(
        id="act_00000000-0000-7000-8000-000000000099",
        task_id=task_id,
        agent_run_id="run_00000000-0000-7000-8000-000000000099",
        principal_agent_id="steward",
        capability="reminder.create",
        schema_version=1,
        resource_type="ReminderCollection",
        resource_id="user_local",
        payload=PAYLOAD,
        payload_hash=payload_hash,
        idempotency_key=compute_idempotency_key(
            user_id="user_local",
            task_id=task_id,
            capability="reminder.create",
            payload_hash=payload_hash,
        ),
        status=ActionStatus.WAITING_APPROVAL,
        created_at=NOW,
        updated_at=NOW,
    )
    async with container.uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        assert task is not None
        running = task.start(lease_owner="w1", now=NOW, lease_seconds=30)
        await uow.tasks.update(running, expected_row_version=task.row_version)
        await uow.agent_runs.add(
            AgentRun(
                id=action.agent_run_id,
                task_id=task_id,
                agent_id="steward",
                agent_version=1,
                attempt=1,
                status=AgentRunStatus.RUNNING,
                context_pack_hash="sha256:" + "c" * 64,
                started_at=NOW,
            )
        )
        await uow.actions.add(action)
        await uow.tasks.update(
            running.wait_for_approval(now=NOW), expected_row_version=running.row_version
        )
        await uow.commit()
    return action


async def test_granting_binds_the_exact_payload_hash(
    container: Container, approvals: ApprovalService
) -> None:
    action = await _waiting_action(container)
    approval = await approvals.grant(
        GrantApproval(action_request_id=action.id, approved_by_user_id="user_local")
    )
    assert approval.status is ApprovalStatus.GRANTED
    assert approval.payload_hash == action.payload_hash
    assert approval.principal_agent_id == "steward"
    assert approval.resource_id == "user_local"
    assert approval.single_use


async def test_granting_requires_an_action_waiting_for_approval(
    container: Container, approvals: ApprovalService
) -> None:
    result = await container.interaction_service.handle(interaction())
    assert result.task_receipt is not None
    with pytest.raises(InvalidRequestError, match="no action request"):
        await approvals.grant(
            GrantApproval(
                action_request_id="act_00000000-0000-7000-8000-0000000000ff",
                approved_by_user_id="user_local",
            )
        )


async def test_consuming_is_atomic_with_returning_the_action_to_executable(
    container: Container, approvals: ApprovalService
) -> None:
    action = await _waiting_action(container)
    approval = await approvals.grant(
        GrantApproval(action_request_id=action.id, approved_by_user_id="user_local")
    )
    consumed, executable = await approvals.consume(ConsumeApproval(approval_id=approval.id))

    assert consumed.status is ApprovalStatus.CONSUMED
    assert executable.status is ActionStatus.VALIDATED

    async with container.uow_factory() as uow:
        stored_approval = await uow.approvals.get(approval.id)
        stored_action = await uow.actions.get(action.id)
        task = await uow.tasks.get(action.task_id)
    assert stored_approval is not None
    assert stored_approval.status is ApprovalStatus.CONSUMED
    assert stored_action is not None
    assert stored_action.status is ActionStatus.VALIDATED
    assert task is not None
    assert task.status is TaskStatus.RUNNING


async def test_a_consumed_approval_cannot_authorize_a_second_action(
    container: Container, approvals: ApprovalService
) -> None:
    action = await _waiting_action(container)
    approval = await approvals.grant(
        GrantApproval(action_request_id=action.id, approved_by_user_id="user_local")
    )
    await approvals.consume(ConsumeApproval(approval_id=approval.id))
    with pytest.raises(InvalidRequestError, match="not_granted"):
        await approvals.consume(ConsumeApproval(approval_id=approval.id))


async def test_an_expired_approval_cannot_be_consumed(
    container: Container, approvals: ApprovalService
) -> None:
    from agtyle.ports.clock import FrozenClock

    action = await _waiting_action(container)
    approval = await approvals.grant(
        GrantApproval(
            action_request_id=action.id, approved_by_user_id="user_local", expires_in_seconds=60
        )
    )
    clock = container.clock
    assert isinstance(clock, FrozenClock)
    clock.advance(timedelta(seconds=61))

    with pytest.raises(InvalidRequestError, match="expired"):
        await approvals.consume(ConsumeApproval(approval_id=approval.id))


async def test_a_modified_action_invalidates_a_held_approval(
    container: Container, approvals: ApprovalService
) -> None:
    """Changing the protected payload after approval must break the binding."""
    action = await _waiting_action(container)
    approval = await approvals.grant(
        GrantApproval(action_request_id=action.id, approved_by_user_id="user_local")
    )
    async with container.uow_factory() as uow:
        stored = await uow.actions.get(action.id)
        assert stored is not None
        tampered = stored.model_copy(
            update={
                "payload": {**PAYLOAD, "title": "transfer the money"},
                "payload_hash": compute_payload_hash(
                    principal_agent_id="steward",
                    capability="reminder.create",
                    resource_type="ReminderCollection",
                    resource_id="user_local",
                    schema_version=1,
                    payload={**PAYLOAD, "title": "transfer the money"},
                ),
                "row_version": stored.row_version + 1,
            }
        )
        await uow.actions.update(tampered, expected_row_version=stored.row_version)
        await uow.commit()

    with pytest.raises(InvalidRequestError, match="payload_hash_mismatch"):
        await approvals.consume(ConsumeApproval(approval_id=approval.id))


async def test_rejecting_terminates_the_task_and_denies_the_action(
    container: Container, approvals: ApprovalService
) -> None:
    action = await _waiting_action(container)
    approval = await approvals.reject(
        RejectApproval(action_request_id=action.id, rejected_by_user_id="user_local")
    )
    assert approval.status is ApprovalStatus.REJECTED

    async with container.uow_factory() as uow:
        stored_action = await uow.actions.get(action.id)
        task = await uow.tasks.get(action.task_id)
        reminders = await uow.reminders.count_all()
    assert stored_action is not None
    assert stored_action.status is ActionStatus.DENIED
    assert task is not None
    assert task.status is TaskStatus.CANCELLED
    assert reminders == 0
