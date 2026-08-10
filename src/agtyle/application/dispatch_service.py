"""Dispatch: turn an Executive proposal into durable, assigned work.

The Intent, the Task, the assignment and both Events commit in a single transaction. Only after
that commit may anything tell the user their work was accepted.
"""

from __future__ import annotations

from agtyle.domain.agents import TaskProposal
from agtyle.domain.common import (
    DomainModel,
    ErrorCode,
    IdPrefix,
    IntentId,
    InvalidRequestError,
    TaskId,
    UnsupportedAssignmentError,
    UtcDatetime,
)
from agtyle.domain.events import Event, EventType, agent_actor, user_actor
from agtyle.domain.intents import Intent
from agtyle.domain.tasks import ExecutionMode, Task, TaskOrigin, TaskStatus
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.registry import AgentRegistryPort
from agtyle.ports.repositories import UnitOfWorkFactory


class TaskReceipt(DomainModel):
    """Proof that assigned work exists in the database, not merely in a response body."""

    task_id: TaskId
    status: TaskStatus
    assigned_agent_id: str
    execution_mode: ExecutionMode
    accepted_at: UtcDatetime


class CreateAndAssignTask(DomainModel):
    """Everything needed to create one Task from one accepted Intent."""

    intent: Intent
    proposal: TaskProposal
    task_id: TaskId
    max_attempts: int
    execution_mode: ExecutionMode | None = None


class DispatchService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        registry: AgentRegistryPort,
        clock: ClockPort,
        ids: IdGeneratorPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._clock = clock
        self._ids = ids

    def validate_proposal(self, proposal: TaskProposal) -> None:
        """Reject a proposal the kernel cannot route before anything is persisted."""
        agent_id = proposal.assigned_agent_id
        if not self._registry.has_agent(agent_id):
            raise UnsupportedAssignmentError(f"unknown agent: {agent_id}")
        manifest = self._registry.agent_manifest(agent_id)
        if not manifest.accepts_type(proposal.task_type):
            raise UnsupportedAssignmentError(
                f"agent {agent_id} does not accept {proposal.task_type!r}"
            )
        for capability in manifest.capabilities.requested:
            if not self._registry.is_capability_registered(capability):
                raise InvalidRequestError(
                    f"agent {agent_id} requests unregistered capability {capability!r}",
                    code=ErrorCode.CONFIGURATION_INVALID,
                )

    def build_task(self, command: CreateAndAssignTask) -> Task:
        now = self._clock.now()
        intent = command.intent
        created = Task(
            id=command.task_id,
            intent_id=intent.id,
            task_type=command.proposal.task_type,
            status=TaskStatus.CREATED,
            execution_mode=command.execution_mode or command.proposal.execution_mode,
            assigned_agent_id=command.proposal.assigned_agent_id,
            objective=command.proposal.objective,
            payload=dict(command.proposal.payload),
            origin=TaskOrigin(
                channel=intent.origin_channel,
                conversation_id=intent.origin_conversation_id,
                user_id=intent.user_id,
            ),
            max_attempts=command.max_attempts,
            created_at=now,
            updated_at=now,
        )
        return created.assign(agent_id=command.proposal.assigned_agent_id, now=now)

    async def create_and_assign(self, command: CreateAndAssignTask) -> TaskReceipt:
        """Atomic operation 1: Intent, Task, assignment and both Events, or nothing at all."""
        self.validate_proposal(command.proposal)
        task = self.build_task(command)
        intent = command.intent

        async with self._uow_factory() as uow:
            await uow.intents.add(intent)
            await uow.tasks.add(task)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.INTENT_ACCEPTED,
                    subject=intent.id,
                    actor=user_actor(intent.user_id),
                    time=task.created_at,
                    data={
                        "channel": intent.origin_channel,
                        "conversation_id": intent.origin_conversation_id,
                        "task_id": task.id,
                    },
                )
            )
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.TASK_ASSIGNED,
                    subject=task.id,
                    actor=agent_actor("executive"),
                    time=task.updated_at,
                    data={
                        "intent_id": intent.id,
                        "assigned_agent_id": task.assigned_agent_id,
                        "task_type": task.task_type,
                        "execution_mode": task.execution_mode.value,
                    },
                )
            )
            await uow.commit()

        return receipt_for(task)

    async def receipt_for_intent(self, intent_id: str) -> TaskReceipt | None:
        """Re-derive the receipt for a replayed interaction from committed state only."""
        async with self._uow_factory() as uow:
            tasks = await uow.tasks.list_by_intent(intent_id)
        return receipt_for(tasks[0]) if tasks else None

    def new_intent_id(self) -> str:
        return self._ids.new_id(IdPrefix.INTENT)

    def new_task_id(self) -> str:
        return self._ids.new_id(IdPrefix.TASK)


def receipt_for(task: Task) -> TaskReceipt:
    return TaskReceipt(
        task_id=task.id,
        status=task.status,
        assigned_agent_id=task.assigned_agent_id,
        execution_mode=task.execution_mode,
        accepted_at=task.updated_at,
    )


__all__ = [
    "CreateAndAssignTask",
    "DispatchService",
    "IntentId",
    "TaskReceipt",
    "receipt_for",
]
