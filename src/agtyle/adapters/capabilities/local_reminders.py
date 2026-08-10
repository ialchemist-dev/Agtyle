"""Local reminder capability.

The adapter creates at most one Reminder per idempotency key. It deliberately does not touch
the source Task: `ExecutionService` owns ActionResult persistence and Task finalization, so a
capability can never mark work complete that the kernel has not verified.
"""

from __future__ import annotations

from agtyle.domain.actions import ActionExecutionStatus, ActionRequest, ReconciliationStatus
from agtyle.domain.common import (
    ActionIdempotencyConflictError,
    CapabilityPermanentError,
    IdPrefix,
)
from agtyle.domain.reminders import (
    Reminder,
    ReminderCreatePayload,
    ReminderStatus,
    normalize_note,
    normalize_title,
    parse_utc_instant,
    require_future_due_time,
)
from agtyle.ports.capability import ActionExecutionResult, ReconciliationResult
from agtyle.ports.clock import ClockPort
from agtyle.ports.id_generator import IdGeneratorPort
from agtyle.ports.repositories import UnitOfWorkFactory

CAPABILITY_NAME = "reminder.create"
SCHEMA_VERSION = 1


class LocalReminderCapability:
    """``CapabilityPort`` implementation backed by the local SQLite reminder table."""

    capability_name = CAPABILITY_NAME
    schema_version = SCHEMA_VERSION

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

    async def execute(self, request: ActionRequest) -> ActionExecutionResult:
        if request.capability != CAPABILITY_NAME:
            raise CapabilityPermanentError(
                f"{CAPABILITY_NAME} adapter received {request.capability!r}",
                task_id=request.task_id,
            )
        if request.schema_version != SCHEMA_VERSION:
            raise CapabilityPermanentError(
                f"unsupported schema version {request.schema_version}", task_id=request.task_id
            )

        payload = self._validated_payload(request)
        now = self._clock.now()
        due = require_future_due_time(parse_utc_instant(payload.scheduled_for_utc), now=now)

        candidate = Reminder(
            id=self._ids.new_id(IdPrefix.REMINDER),
            user_id=request.resource_id,
            source_task_id=request.task_id,
            title=normalize_title(payload.title),
            note=normalize_note(payload.note),
            scheduled_for_utc=due,
            timezone=payload.timezone,
            status=ReminderStatus.SCHEDULED,
            idempotency_key=request.idempotency_key,
            created_at=now,
            updated_at=now,
        )

        async with self._uow_factory() as uow:
            existing = await uow.reminders.get_by_idempotency_key(
                user_id=candidate.user_id, key=request.idempotency_key
            )
            if existing is not None:
                await uow.rollback()
                if not existing.matches_protected_fields(candidate):
                    # The same key with a different meaning is a conflict, never an overwrite.
                    raise ActionIdempotencyConflictError(
                        "a reminder already exists for this idempotency key with different "
                        "protected fields",
                        task_id=request.task_id,
                    )
                return ActionExecutionResult(
                    status=ActionExecutionStatus.ALREADY_APPLIED,
                    external_ref=existing.id,
                    result={
                        "reminder_id": existing.id,
                        "status": existing.status.value,
                        "scheduled_for_utc": existing.scheduled_for_utc.isoformat(),
                        "timezone": existing.timezone,
                    },
                    reconciliation_status=ReconciliationStatus.RECONCILED,
                )

            await uow.reminders.add(candidate)
            await uow.commit()

        return ActionExecutionResult(
            status=ActionExecutionStatus.APPLIED,
            external_ref=candidate.id,
            result={
                "reminder_id": candidate.id,
                "status": candidate.status.value,
                "scheduled_for_utc": candidate.scheduled_for_utc.isoformat(),
                "timezone": candidate.timezone,
            },
            reconciliation_status=ReconciliationStatus.NOT_REQUIRED,
        )

    async def reconcile(self, idempotency_key: str) -> ReconciliationResult:
        """Report whether a prior attempt already created the effect for this key."""
        async with self._uow_factory() as uow:
            action = await uow.actions.get_by_idempotency_key(idempotency_key)
            if action is None:
                return ReconciliationResult(found=False)
            reminder = await uow.reminders.get_by_idempotency_key(
                user_id=action.resource_id, key=idempotency_key
            )
        if reminder is None:
            return ReconciliationResult(found=False)
        return ReconciliationResult(
            found=True,
            external_ref=reminder.id,
            result={"reminder_id": reminder.id, "status": reminder.status.value},
        )

    @staticmethod
    def _validated_payload(request: ActionRequest) -> ReminderCreatePayload:
        try:
            return ReminderCreatePayload(**request.payload)
        except Exception as exc:
            raise CapabilityPermanentError(
                f"reminder payload rejected by the adapter: {exc}", task_id=request.task_id
            ) from exc
