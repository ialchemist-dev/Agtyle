"""Execution: claim a Task, run its Agent, authorize the proposed Action, apply it, finalize.

The sequence is deliberately explicit, because each arrow is a place where a crash must not
lose or duplicate an effect:

```text
claim Task -> ContextPack -> Agent runtime -> validate output -> persist ActionRequest
    -> schema validation -> Cedar -> Capability (only on ALLOW) -> ActionResult
    -> Task completion + Event + Notification, all in one transaction
```

Nothing here calls a Capability Adapter directly except through the ``ALLOW`` branch, and no
Agent runtime is ever handed an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from agtyle.application.context_builder import build_context_pack, context_pack_hash
from agtyle.application.policy_service import EvaluateAction, PolicyService
from agtyle.application.retry_policy import FailureClass, RetryPolicy, classify
from agtyle.domain.actions import (
    ActionExecutionStatus,
    ActionRequest,
    ActionResult,
    ActionStatus,
    PolicyOutcome,
    compute_idempotency_key,
    compute_payload_hash,
)
from agtyle.domain.agents import (
    ActionRequestProposal,
    AgentAssignment,
    AgentRun,
    AgentRunStatus,
    ClarificationResponse,
    ContextPack,
    DirectResponse,
    TrustLabel,
)
from agtyle.domain.common import (
    AgtyleError,
    ApprovalRequiredError,
    CapabilityPermanentError,
    CapabilityTransientError,
    DomainModel,
    ErrorCode,
    IdPrefix,
    InvalidAgentOutputError,
    LeaseLostError,
    PolicyDeniedError,
    PolicyEngineError,
    UnsupportedAssignmentError,
    redact_secrets,
)
from agtyle.domain.events import Event, EventType, agent_actor, system_actor, worker_actor
from agtyle.domain.notifications import (
    DeliveryStatus,
    Notification,
    NotificationDestination,
    NotificationKind,
    task_terminal_delivery_key,
)
from agtyle.domain.tasks import Task, TaskStatus
from agtyle.observability.logging import LogContext, get_logger
from agtyle.ports.agent_runtime import AgentRuntimePort
from agtyle.ports.capability import ActionExecutionResult, CapabilityPort
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.registry import AgentRegistryPort
from agtyle.ports.repositories import UnitOfWorkFactory
from agtyle.ports.schema_registry import ActionSchemaPort

logger = get_logger(__name__)


class ExecutionOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    WAITING_APPROVAL = "waiting_approval"
    LEASE_LOST = "lease_lost"


class ExecutionReport(DomainModel):
    """What one attempt did, expressed so a Worker and a test can both assert on it."""

    outcome: ExecutionOutcome
    task_id: str
    agent_run_id: str | None = None
    action_request_id: str | None = None
    policy_outcome: PolicyOutcome | None = None
    reminder_id: str | None = None
    error_code: ErrorCode | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ClaimedTask:
    task: Task
    agent_run: AgentRun


class ExecutionService:
    """Owns Task state transitions, ActionResult persistence and terminal Notifications."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        registry: AgentRegistryPort,
        policy_service: PolicyService,
        schemas: ActionSchemaPort,
        agent_runtimes: dict[str, AgentRuntimePort],
        capabilities: dict[str, CapabilityPort],
        clock: ClockPort,
        ids: IdGeneratorPort,
        retry_policy: RetryPolicy,
        lease_seconds: int,
        notification_adapter: str,
        max_notification_attempts: int,
        user_preferences: dict[str, Any] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._policy = policy_service
        self._schemas = schemas
        self._runtimes = agent_runtimes
        self._capabilities = capabilities
        self._clock = clock
        self._ids = ids
        self._retry = retry_policy
        self._lease_seconds = lease_seconds
        self._notification_adapter = notification_adapter
        self._max_notification_attempts = max_notification_attempts
        self._preferences = user_preferences or {}

    # ----------------------------------------------------------------------------------
    # Claim
    # ----------------------------------------------------------------------------------

    async def claim_task(self, *, owner: str) -> ClaimedTask | None:
        """Atomic operation 2: claim, increment the attempt, create the AgentRun, start.

        The transaction commits before any Agent runtime is invoked, so a crash during the
        Agent call leaves a durable record that the attempt happened.
        """
        now = self._clock.now()
        async with self._uow_factory() as uow:
            task = await uow.tasks.claim_next_assigned(
                owner=owner, now=now, lease_seconds=self._lease_seconds
            )
            if task is None:
                await uow.rollback()
                return None

            manifest = self._registry.agent_manifest(task.assigned_agent_id)
            context = self._context_for(
                task, manifest.context_scopes, manifest.capabilities.requested
            )
            run = AgentRun(
                id=self._ids.new_id(IdPrefix.AGENT_RUN),
                task_id=task.id,
                agent_id=task.assigned_agent_id,
                agent_version=manifest.version,
                attempt=task.attempt_count,
                status=AgentRunStatus.RUNNING,
                context_pack_hash=context_pack_hash(context),
                started_at=now,
            )
            await uow.agent_runs.add(run)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.TASK_STARTED,
                    subject=task.id,
                    actor=worker_actor(owner),
                    time=now,
                    data={"agent_run_id": run.id, "attempt": run.attempt},
                )
            )
            await uow.commit()
        return ClaimedTask(task=task, agent_run=run)

    async def heartbeat(self, task: Task, *, owner: str) -> Task:
        """Renew the lease. Only the owning Worker may renew, and only while it still owns it."""
        now = self._clock.now()
        renewed = task.heartbeat(lease_owner=owner, now=now, lease_seconds=self._lease_seconds)
        async with self._uow_factory() as uow:
            current = await uow.tasks.get(task.id)
            if current is None or not current.owns_lease(owner, now=now):
                await uow.rollback()
                raise LeaseLostError("lease is no longer held by this worker", task_id=task.id)
            renewed = current.heartbeat(
                lease_owner=owner, now=now, lease_seconds=self._lease_seconds
            )
            await uow.tasks.update(renewed, expected_row_version=current.row_version)
            await uow.commit()
        return renewed

    # ----------------------------------------------------------------------------------
    # Execute
    # ----------------------------------------------------------------------------------

    async def execute_claimed_task(self, claimed: ClaimedTask, *, owner: str) -> ExecutionReport:
        task = claimed.task
        run = claimed.agent_run
        with LogContext(task_id=task.id, agent_run_id=run.id, worker_id=owner):
            try:
                return await self._execute(claimed, owner=owner)
            except AgtyleError as error:
                logger.warning(
                    "task attempt failed",
                    extra={"error_code": error.code.value, "failure": error.detail},
                )
                return await self.finalize_failure(claimed, owner=owner, error=error)

    async def _execute(self, claimed: ClaimedTask, *, owner: str) -> ExecutionReport:
        task = claimed.task
        run = claimed.agent_run

        manifest = self._registry.agent_manifest(task.assigned_agent_id)
        runtime = self._runtimes.get(task.assigned_agent_id)
        if runtime is None:
            raise UnsupportedAssignmentError(
                f"no runtime registered for agent {task.assigned_agent_id}", task_id=task.id
            )

        context = self._context_for(task, manifest.context_scopes, manifest.capabilities.requested)
        assignment = AgentAssignment(
            assignment_type=task.task_type,
            agent_id=task.assigned_agent_id,
            task_id=task.id,
            agent_run_id=run.id,
            payload={"user_id": task.origin.user_id},
        )

        output = await runtime.run(assignment, context)

        # Agent output is untrusted structured input. Anything that is not exactly one
        # ActionRequest proposal ends the attempt without touching a Capability.
        if isinstance(output, ClarificationResponse):
            raise InvalidAgentOutputError(
                f"agent requires clarification: {output.message}", task_id=task.id
            )
        if isinstance(output, DirectResponse):
            raise InvalidAgentOutputError(
                "agent returned a direct response for a delegated action task", task_id=task.id
            )
        if not isinstance(output, ActionRequestProposal):
            raise InvalidAgentOutputError("agent produced an unsupported output", task_id=task.id)

        action = self._materialize_action(task, run, output)
        action = await self._persist_action(task, action)

        evaluation = await self._authorize(task, action)

        if evaluation.outcome is PolicyOutcome.REQUIRE_APPROVAL:
            return await self._park_for_approval(claimed, action, owner=owner)
        if evaluation.outcome is PolicyOutcome.DENY:
            raise PolicyDeniedError(
                f"policy denied {action.capability}: {evaluation.decision.reason_code}",
                task_id=task.id,
            )
        if evaluation.outcome is PolicyOutcome.ERROR:
            raise PolicyEngineError(
                f"authorization could not be decided: {evaluation.decision.reason_code}",
                task_id=task.id,
            )

        capability = self._capabilities.get(action.capability)
        if capability is None:
            raise CapabilityPermanentError(
                f"no adapter registered for capability {action.capability}", task_id=task.id
            )

        started_at = self._clock.now()
        execution = await capability.execute(action)
        return await self._finalize_success(
            claimed, action, execution, owner=owner, started_at=started_at
        )

    # ----------------------------------------------------------------------------------
    # Steps
    # ----------------------------------------------------------------------------------

    def _context_for(
        self, task: Task, context_scopes: list[str], authority_budget: list[str]
    ) -> ContextPack:
        preferences = {
            **self._preferences,
            "channel": task.origin.channel,
            "conversation_id": task.origin.conversation_id,
        }
        return build_context_pack(
            task=task,
            context_scopes=context_scopes,
            authority_budget=authority_budget,
            available_preferences=preferences,
            references=[f"interaction://{task.origin.conversation_id}"],
            trust_labels={f"interaction://{task.origin.conversation_id}": TrustLabel.USER_DIRECT},
        )

    def _materialize_action(
        self, task: Task, run: AgentRun, proposal: ActionRequestProposal
    ) -> ActionRequest:
        """The kernel — never the Agent — supplies identity, hash, idempotency key and status."""
        now = self._clock.now()
        payload_hash = compute_payload_hash(
            principal_agent_id=task.assigned_agent_id,
            capability=proposal.capability,
            resource_type=proposal.resource_type,
            resource_id=proposal.resource_id,
            schema_version=proposal.schema_version,
            payload=proposal.payload,
        )
        return ActionRequest(
            id=self._ids.new_id(IdPrefix.ACTION_REQUEST),
            task_id=task.id,
            agent_run_id=run.id,
            principal_agent_id=task.assigned_agent_id,
            capability=proposal.capability,
            schema_version=proposal.schema_version,
            resource_type=proposal.resource_type,
            resource_id=proposal.resource_id,
            payload=proposal.payload,
            payload_hash=payload_hash,
            idempotency_key=compute_idempotency_key(
                user_id=task.origin.user_id,
                task_id=task.id,
                capability=proposal.capability,
                payload_hash=payload_hash,
            ),
            status=ActionStatus.PROPOSED,
            created_at=now,
            updated_at=now,
        )

    async def _persist_action(self, task: Task, action: ActionRequest) -> ActionRequest:
        """Atomic operation 3: persist the proposal and the result of schema validation.

        A retry of the same Task and payload produces the same idempotency key, so the existing
        ActionRequest is reused rather than duplicated.
        """
        schema_result = self._schemas.validate_payload(
            capability=action.capability,
            schema_version=action.schema_version,
            payload=action.payload,
        )
        now = self._clock.now()
        async with self._uow_factory() as uow:
            existing = await uow.actions.get_by_idempotency_key(action.idempotency_key)
            if existing is not None:
                await uow.rollback()
                return existing

            stored = action.model_copy(
                update={
                    "status": (
                        ActionStatus.VALIDATED if schema_result.valid else ActionStatus.FAILED
                    ),
                    "updated_at": now,
                }
            )
            await uow.actions.add(stored)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.ACTION_PROPOSED,
                    subject=task.id,
                    actor=agent_actor(task.assigned_agent_id),
                    time=now,
                    data={
                        "action_request_id": stored.id,
                        "capability": stored.capability,
                        "payload_hash": stored.payload_hash,
                        "schema_valid": schema_result.valid,
                    },
                )
            )
            await uow.commit()

        if not schema_result.valid:
            raise AgtyleError(
                "; ".join(schema_result.errors) or "action payload failed schema validation",
                code=ErrorCode.ACTION_SCHEMA_INVALID,
                task_id=task.id,
            )
        return stored

    async def _authorize(self, task: Task, action: ActionRequest) -> Any:
        """Atomic operation 4: store the PolicyDecision and move the ActionRequest with it."""
        approval = None
        async with self._uow_factory() as uow:
            approvals = await uow.approvals.list_for_action(action.id)
            approval = approvals[-1] if approvals else None

        evaluation = await self._policy.evaluate(
            EvaluateAction(
                action_request=action,
                origin_user_id=task.origin.user_id,
                has_direct_user_instruction=True,
                approval=approval,
            )
        )

        now = self._clock.now()
        next_status = {
            PolicyOutcome.ALLOW: ActionStatus.AUTHORIZED,
            PolicyOutcome.REQUIRE_APPROVAL: ActionStatus.WAITING_APPROVAL,
            PolicyOutcome.DENY: ActionStatus.DENIED,
            PolicyOutcome.ERROR: ActionStatus.PROPOSED,
        }[evaluation.outcome]

        async with self._uow_factory() as uow:
            current = await uow.actions.get(action.id)
            if current is None:  # pragma: no cover - the action was just committed
                raise CapabilityPermanentError("action request disappeared", task_id=task.id)
            await uow.actions.add_policy_decision(evaluation.decision)
            updated = current.with_status(next_status, now=now)
            await uow.actions.update(updated, expected_row_version=current.row_version)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=(
                        EventType.ACTION_AUTHORIZED
                        if evaluation.outcome is PolicyOutcome.ALLOW
                        else EventType.ACTION_DENIED
                    ),
                    subject=task.id,
                    actor=system_actor(),
                    time=now,
                    data={
                        "action_request_id": action.id,
                        "decision": evaluation.outcome.value,
                        "cedar_decision": evaluation.decision.cedar_decision.value,
                        "reason_code": evaluation.decision.reason_code,
                        "policy_ids": evaluation.decision.determining_policy_ids,
                        "policy_decision_id": evaluation.decision.id,
                    },
                )
            )
            await uow.commit()
        return evaluation

    async def _park_for_approval(
        self, claimed: ClaimedTask, action: ActionRequest, *, owner: str
    ) -> ExecutionReport:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            current = await uow.tasks.get(claimed.task.id)
            if current is None or not current.owns_lease(owner, now=now):
                await uow.rollback()
                raise LeaseLostError(
                    "lease lost before parking for approval", task_id=claimed.task.id
                )
            waiting = current.wait_for_approval(now=now)
            await uow.tasks.update(waiting, expected_row_version=current.row_version)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.APPROVAL_REQUESTED,
                    subject=claimed.task.id,
                    actor=system_actor(),
                    time=now,
                    data={"action_request_id": action.id, "payload_hash": action.payload_hash},
                )
            )
            await uow.commit()
        return ExecutionReport(
            outcome=ExecutionOutcome.WAITING_APPROVAL,
            task_id=claimed.task.id,
            agent_run_id=claimed.agent_run.id,
            action_request_id=action.id,
            policy_outcome=PolicyOutcome.REQUIRE_APPROVAL,
        )

    async def _finalize_success(
        self,
        claimed: ClaimedTask,
        action: ActionRequest,
        execution: ActionExecutionResult,
        *,
        owner: str,
        started_at: datetime,
    ) -> ExecutionReport:
        """Atomic operation 5: ActionResult, AgentRun, Task, Events and Notification together.

        If this transaction rolls back, the Task is not complete, there is no orphan
        Notification, and the capability's own idempotency key lets the retry observe the
        prior effect instead of creating a second one.
        """
        task = claimed.task
        run = claimed.agent_run
        now = self._clock.now()

        if execution.status is ActionExecutionStatus.FAILED:
            raise CapabilityTransientError(
                f"capability {action.capability} reported failure", task_id=task.id
            )

        async with self._uow_factory() as uow:
            current = await uow.tasks.get(task.id)
            if current is None or not current.owns_lease(owner, now=now):
                await uow.rollback()
                raise LeaseLostError(
                    "lease lost before the completion transaction", task_id=task.id
                )

            if await uow.actions.get_result(action.id) is None:
                await uow.actions.add_result(
                    ActionResult(
                        id=self._ids.new_id(IdPrefix.ACTION_RESULT),
                        action_request_id=action.id,
                        status=execution.status,
                        external_ref=execution.external_ref,
                        result=execution.result,
                        reconciliation_status=execution.reconciliation_status,
                        started_at=started_at,
                        completed_at=now,
                        created_at=now,
                    )
                )

            stored_action = await uow.actions.get(action.id)
            assert stored_action is not None
            await uow.actions.update(
                stored_action.with_status(ActionStatus.APPLIED, now=now),
                expected_row_version=stored_action.row_version,
            )
            await uow.agent_runs.update(run.succeed(now=now))

            completed = current.complete(now=now)
            await uow.tasks.update(completed, expected_row_version=current.row_version)

            reminder_id = execution.result.get("reminder_id")
            if reminder_id:
                await uow.events.append(
                    Event(
                        id=self._ids.new_id(IdPrefix.EVENT),
                        type=EventType.REMINDER_CREATED,
                        subject=task.id,
                        actor=system_actor(),
                        time=now,
                        data={
                            "reminder_id": reminder_id,
                            "action_request_id": action.id,
                            "already_applied": execution.status
                            is ActionExecutionStatus.ALREADY_APPLIED,
                        },
                    )
                )
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.TASK_COMPLETED,
                    subject=task.id,
                    actor=agent_actor(task.assigned_agent_id),
                    time=now,
                    data={"action_request_id": action.id, "result_ref": reminder_id},
                )
            )

            await self._create_terminal_notification(
                uow,
                task=completed,
                status=TaskStatus.COMPLETED,
                payload={
                    "task_id": task.id,
                    "status": TaskStatus.COMPLETED.value,
                    "result_ref": reminder_id,
                    "summary_code": "reminder_created",
                },
                now=now,
            )
            await uow.commit()

        return ExecutionReport(
            outcome=ExecutionOutcome.COMPLETED,
            task_id=task.id,
            agent_run_id=run.id,
            action_request_id=action.id,
            policy_outcome=PolicyOutcome.ALLOW,
            reminder_id=str(reminder_id) if reminder_id else None,
        )

    async def finalize_failure(
        self, claimed: ClaimedTask, *, owner: str, error: AgtyleError
    ) -> ExecutionReport:
        """Classify the failure, then either schedule a retry or fail the Task terminally."""
        task = claimed.task
        run = claimed.agent_run
        now = self._clock.now()
        failure = self._failure_class(error)
        safe_detail = redact_secrets(error.detail)[:500]

        if failure is FailureClass.LEASE_LOST:
            return ExecutionReport(
                outcome=ExecutionOutcome.LEASE_LOST,
                task_id=task.id,
                agent_run_id=run.id,
                error_code=error.code,
                detail=safe_detail,
            )

        async with self._uow_factory() as uow:
            current = await uow.tasks.get(task.id)
            if current is None or not current.owns_lease(owner, now=now):
                await uow.rollback()
                return ExecutionReport(
                    outcome=ExecutionOutcome.LEASE_LOST,
                    task_id=task.id,
                    agent_run_id=run.id,
                    error_code=ErrorCode.LEASE_LOST,
                    detail="another worker recovered this task",
                )

            retry = self._retry.should_retry(
                failure, attempt_count=current.attempt_count, max_attempts=current.max_attempts
            )
            failed = current.fail(
                now=now,
                error_code=error.code,
                error_message=safe_detail,
                next_attempt_at=(
                    self._retry.next_attempt_at(
                        now=now, attempt_count=current.attempt_count, seed=task.id
                    )
                    if retry
                    else None
                ),
            )
            await uow.agent_runs.update(
                run.fail(now=now, error_code=error.code, error_message=safe_detail)
            )
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.AGENT_RUN_FAILED,
                    subject=task.id,
                    actor=agent_actor(task.assigned_agent_id),
                    time=now,
                    data={
                        "agent_run_id": run.id,
                        "error_code": error.code.value,
                        "failure_class": failure.value,
                        "retry": retry,
                    },
                )
            )

            if retry:
                reassigned = failed.reassign_for_retry(now=now)
                await uow.tasks.update(reassigned, expected_row_version=current.row_version)
                await uow.commit()
                return ExecutionReport(
                    outcome=ExecutionOutcome.RETRY_SCHEDULED,
                    task_id=task.id,
                    agent_run_id=run.id,
                    error_code=error.code,
                    detail=safe_detail,
                )

            await uow.tasks.update(failed, expected_row_version=current.row_version)
            await uow.events.append(
                Event(
                    id=self._ids.new_id(IdPrefix.EVENT),
                    type=EventType.TASK_FAILED,
                    subject=task.id,
                    actor=system_actor(),
                    time=now,
                    data={"error_code": error.code.value, "failure_class": failure.value},
                )
            )
            await self._create_terminal_notification(
                uow,
                task=failed,
                status=TaskStatus.FAILED,
                payload={
                    "task_id": task.id,
                    "status": TaskStatus.FAILED.value,
                    "error_code": error.code.value,
                    "summary_code": "task_failed",
                },
                now=now,
            )
            await uow.commit()

        return ExecutionReport(
            outcome=ExecutionOutcome.FAILED,
            task_id=task.id,
            agent_run_id=run.id,
            error_code=error.code,
            detail=safe_detail,
        )

    async def _create_terminal_notification(
        self, uow: Any, *, task: Task, status: TaskStatus, payload: dict[str, Any], now: datetime
    ) -> None:
        """One terminal Notification per Task outcome, guaranteed by the stable delivery key."""
        delivery_key = task_terminal_delivery_key(task.id, status)
        if await uow.notifications.get_by_delivery_key(delivery_key) is not None:
            return
        await uow.notifications.add(
            Notification(
                id=self._ids.new_id(IdPrefix.NOTIFICATION),
                task_id=task.id,
                kind=(
                    NotificationKind.TASK_COMPLETED
                    if status is TaskStatus.COMPLETED
                    else NotificationKind.TASK_FAILED
                ),
                destination=NotificationDestination(
                    adapter=self._notification_adapter,
                    user_id=task.origin.user_id,
                    conversation_id=task.origin.conversation_id,
                ),
                payload=payload,
                delivery_key=delivery_key,
                delivery_status=DeliveryStatus.PENDING,
                max_attempts=self._max_notification_attempts,
                created_at=now,
                updated_at=now,
            )
        )
        await uow.events.append(
            Event(
                id=self._ids.new_id(IdPrefix.EVENT),
                type=EventType.NOTIFICATION_CREATED,
                subject=task.id,
                actor=system_actor(),
                time=now,
                data={"delivery_key": delivery_key, "kind": payload.get("summary_code")},
            )
        )

    @staticmethod
    def _failure_class(error: AgtyleError) -> FailureClass:
        if isinstance(error, LeaseLostError):
            return FailureClass.LEASE_LOST
        if isinstance(error, PolicyEngineError):
            return FailureClass.AUTHORIZATION_ERROR
        if isinstance(error, (PolicyDeniedError, ApprovalRequiredError)):
            return FailureClass.AUTHORIZATION_DENIED
        if isinstance(error, CapabilityTransientError):
            return FailureClass.CAPABILITY_TRANSIENT
        if isinstance(error, CapabilityPermanentError):
            return FailureClass.CAPABILITY_PERMANENT
        return classify(error.code)
