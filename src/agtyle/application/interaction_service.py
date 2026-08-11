"""Interaction handling: the front door.

Responsibilities, in order:

1. enforce interaction idempotency, so a retried request returns the original receipt and a
   reused key with a different body is a conflict;
2. ask the Executive Agent what should happen;
3. persist Intent and, when work is required, Task and assignment atomically;
4. return a receipt only after that transaction has committed.

Interactive execution runs the same Task through the same Cedar path. If the interactive budget
runs out before the capability is invoked, the *same* Task is converted to delegated. A second
Task is never created.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

from pydantic import Field

from agtyle.application.dispatch_service import (
    CreateAndAssignTask,
    DispatchService,
    TaskReceipt,
    receipt_for,
)
from agtyle.application.execution_service import ExecutionOutcome, ExecutionService
from agtyle.domain.agents import (
    AgentAssignment,
    AgentRun,
    AgentRunStatus,
    ClarificationResponse,
    ContextPack,
    ContextTask,
    DirectResponse,
    TaskProposal,
    TrustLabel,
)
from agtyle.domain.common import (
    DomainModel,
    IdPrefix,
    InteractionIdempotencyConflictError,
    InvalidRequestError,
    JsonMapping,
    UserId,
)
from agtyle.domain.events import Event, EventType, user_actor
from agtyle.domain.intents import Intent, interaction_request_hash
from agtyle.domain.tasks import ExecutionMode
from agtyle.observability.logging import LogContext, get_logger
from agtyle.ports.agent_runtime import AgentRuntimePort
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.repositories import UnitOfWorkFactory

logger = get_logger(__name__)


class InteractionOutcome(StrEnum):
    TASK_ASSIGNED = "task_assigned"
    DIRECT_RESPONSE = "direct_response"
    CLARIFICATION_REQUIRED = "clarification_required"
    COMPLETED = "completed"


class HandleInteraction(DomainModel):
    user_id: UserId
    conversation_id: str
    channel: str
    input: str
    idempotency_key: str
    preferred_execution_mode: ExecutionMode = ExecutionMode.DELEGATED

    def request_hash(self) -> str:
        return interaction_request_hash(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            channel=self.channel,
            original_input=self.input,
            preferred_execution_mode=self.preferred_execution_mode.value,
        )


class InteractionResult(DomainModel):
    outcome: InteractionOutcome
    intent_id: str
    message: str
    task_receipt: TaskReceipt | None = None
    missing: list[str] = Field(default_factory=list)
    result: JsonMapping | None = None
    replayed: bool = False


class InteractionService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        dispatch: DispatchService,
        execution: ExecutionService,
        executive_runtime: AgentRuntimePort,
        clock: ClockPort,
        ids: IdGeneratorPort,
        max_task_attempts: int,
        interactive_budget_seconds: float,
        user_preferences: JsonMapping | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._dispatch = dispatch
        self._execution = execution
        self._executive = executive_runtime
        self._clock = clock
        self._ids = ids
        self._max_task_attempts = max_task_attempts
        self._interactive_budget = interactive_budget_seconds
        self._preferences = dict(user_preferences or {})

    async def handle(self, command: HandleInteraction) -> InteractionResult:
        replay = await self._replay(command)
        if replay is not None:
            return replay

        intent_id = self._ids.new_id(IdPrefix.INTENT)
        task_id = self._ids.new_id(IdPrefix.TASK)

        with LogContext(intent_id=intent_id):
            intent = Intent(
                id=intent_id,
                user_id=command.user_id,
                origin_channel=command.channel,
                origin_conversation_id=command.conversation_id,
                interaction_idempotency_key=command.idempotency_key,
                request_hash=command.request_hash(),
                original_input=command.input,
                created_at=self._clock.now(),
            )

            output = await self._executive.run(
                AgentAssignment(
                    assignment_type="interaction",
                    agent_id="executive",
                    input_text=command.input,
                    payload={"user_id": command.user_id},
                ),
                self._interaction_context(command, task_id),
            )

            if isinstance(output, ClarificationResponse):
                await self._persist_intent_only(intent, note="clarification_required")
                return InteractionResult(
                    outcome=InteractionOutcome.CLARIFICATION_REQUIRED,
                    intent_id=intent.id,
                    message=output.message,
                    missing=list(output.missing),
                )

            if isinstance(output, DirectResponse):
                await self._persist_intent_only(intent, note="direct_response")
                return InteractionResult(
                    outcome=InteractionOutcome.DIRECT_RESPONSE,
                    intent_id=intent.id,
                    message=output.message,
                )

            if not isinstance(output, TaskProposal):
                raise InvalidRequestError("the Executive Agent produced an unsupported output")

            interactive = command.preferred_execution_mode is ExecutionMode.INTERACTIVE
            receipt = await self._dispatch.create_and_assign(
                CreateAndAssignTask(
                    intent=intent,
                    proposal=output,
                    task_id=task_id,
                    max_attempts=self._max_task_attempts,
                    execution_mode=(
                        ExecutionMode.INTERACTIVE if interactive else output.execution_mode
                    ),
                )
            )

            if interactive:
                return await self._run_interactive(intent, receipt)

            return InteractionResult(
                outcome=InteractionOutcome.TASK_ASSIGNED,
                intent_id=intent.id,
                message=(
                    f"The {output.task_type.replace('_', ' ')} task has been assigned to the "
                    f"{output.assigned_agent_id.capitalize()} Agent."
                ),
                task_receipt=receipt,
            )

    # ----------------------------------------------------------------------------------

    async def _replay(self, command: HandleInteraction) -> InteractionResult | None:
        """A repeated interaction must return the original receipt and create nothing new."""
        async with self._uow_factory() as uow:
            existing = await uow.intents.find_by_idempotency_key(
                user_id=command.user_id, key=command.idempotency_key
            )
        if existing is None:
            return None
        if existing.request_hash != command.request_hash():
            raise InteractionIdempotencyConflictError(
                "this idempotency key was already used with a different request body"
            )

        receipt = await self._dispatch.receipt_for_intent(existing.id)
        if receipt is None:
            return InteractionResult(
                outcome=InteractionOutcome.DIRECT_RESPONSE,
                intent_id=existing.id,
                message="This interaction was already handled and required no durable work.",
                replayed=True,
            )
        return InteractionResult(
            outcome=InteractionOutcome.TASK_ASSIGNED,
            intent_id=existing.id,
            message="This interaction was already accepted; returning the original receipt.",
            task_receipt=receipt,
            replayed=True,
        )

    async def _persist_intent_only(self, intent: Intent, *, note: str) -> None:
        async with self._uow_factory() as uow:
            await uow.intents.add(intent)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.INTENT_ACCEPTED,
                    subject=intent.id,
                    actor=user_actor(intent.user_id),
                    time=intent.created_at,
                    data={"outcome": note, "conversation_id": intent.origin_conversation_id},
                )
            )
            await uow.commit()

    def _interaction_context(self, command: HandleInteraction, task_id: str) -> ContextPack:
        reference = f"interaction://{command.conversation_id}"
        return ContextPack(
            task=ContextTask(
                id=task_id,
                task_type="interaction",
                objective="Interpret the interaction and route it",
                payload={},
            ),
            references=[reference],
            user_preferences={
                key: value for key, value in self._preferences.items() if key in {"timezone"}
            },
            authority_budget=[],
            trust_labels={reference: TrustLabel.USER_DIRECT},
        )

    async def _run_interactive(self, intent: Intent, receipt: TaskReceipt) -> InteractionResult:
        """Run the same Task inline, converting it to delegated if the budget runs out.

        The conversion keeps the same Task id, so the Worker continues the very work the user
        was already told about.
        """
        owner = f"interactive:{receipt.task_id}"
        try:
            claimed = await asyncio.wait_for(
                self._execution.claim_task(owner=owner), timeout=self._interactive_budget
            )
        except TimeoutError:
            claimed = None

        if claimed is None or claimed.task.id != receipt.task_id:
            return await self._convert_to_delegated(intent, receipt)

        try:
            report = await asyncio.wait_for(
                self._execution.execute_claimed_task(claimed, owner=owner),
                timeout=self._interactive_budget,
            )
        except TimeoutError:
            await self._abandon_interactive_run(claimed.agent_run)
            return await self._convert_to_delegated(intent, receipt)

        if report.outcome is not ExecutionOutcome.COMPLETED:
            return InteractionResult(
                outcome=InteractionOutcome.TASK_ASSIGNED,
                intent_id=intent.id,
                message=(
                    "The task did not complete interactively and will continue in the background."
                ),
                task_receipt=receipt,
            )

        async with self._uow_factory() as uow:
            task = await uow.tasks.get(receipt.task_id)
        assert task is not None
        return InteractionResult(
            outcome=InteractionOutcome.COMPLETED,
            intent_id=intent.id,
            message="The reminder was created.",
            task_receipt=receipt_for(task),
            result={"reminder_id": report.reminder_id, "task_id": report.task_id},
        )

    async def _abandon_interactive_run(self, run: AgentRun) -> None:
        """The interactive attempt is over; its AgentRun must not stay `running` forever."""
        async with self._uow_factory() as uow:
            current = await uow.agent_runs.get(run.id)
            if current is not None and current.status is AgentRunStatus.RUNNING:
                await uow.agent_runs.update(current.abandon(now=self._clock.now()))
                await uow.commit()
            else:
                await uow.rollback()

    async def _convert_to_delegated(
        self, intent: Intent, receipt: TaskReceipt
    ) -> InteractionResult:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            task = await uow.tasks.get(receipt.task_id)
            if task is None:  # pragma: no cover - the task was just committed
                await uow.rollback()
                raise InvalidRequestError("the interactive task disappeared")
            if task.execution_mode is ExecutionMode.INTERACTIVE:
                converted = task.convert_to_delegated(now=now)
                await uow.tasks.update(converted, expected_row_version=task.row_version)
                await uow.events.append(
                    Event(
                        id=self._ids.new_id(IdPrefix.EVENT),
                        type=EventType.TASK_CONVERTED_TO_DELEGATED,
                        subject=task.id,
                        actor=user_actor(intent.user_id),
                        time=now,
                        data={"reason": "interactive_deadline_reached"},
                    )
                )
                await uow.commit()
                task = converted
            else:
                await uow.rollback()

        return InteractionResult(
            outcome=InteractionOutcome.TASK_ASSIGNED,
            intent_id=intent.id,
            message=(
                "This is taking longer than the interactive budget, so the same task will "
                "continue in the background and you will be notified when it completes."
            ),
            task_receipt=receipt_for(task),
        )
