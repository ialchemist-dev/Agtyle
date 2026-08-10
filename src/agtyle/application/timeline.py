"""The Task timeline read model.

A timeline answers "why did this happen?" by assembling current state and the historical
records that produced it. Current state and history are labelled separately so nobody mistakes
an Event ledger for the source of truth: Events explain, Tasks decide.
"""

from __future__ import annotations

from pydantic import Field

from agtyle.domain.common import DomainModel, JsonMapping
from agtyle.ports.repositories import UnitOfWorkFactory


class TimelineEntry(DomainModel):
    """One historical record, normalized so a client can render a single ordered list."""

    at: str
    kind: str
    reference: str
    summary: str
    data: JsonMapping = Field(default_factory=dict)


class TaskTimeline(DomainModel):
    task_id: str
    current_state: JsonMapping
    history: list[TimelineEntry]
    record_counts: dict[str, int]


class TimelineService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def for_task(self, task_id: str) -> TaskTimeline | None:
        async with self._uow_factory() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                return None

            runs = await uow.agent_runs.list_for_task(task_id)
            actions = await uow.actions.list_for_task(task_id)
            reminders = await uow.reminders.list_for_task(task_id)
            notifications = await uow.notifications.list_for_task(task_id)
            subjects = [task_id, task.intent_id, *[reminder.id for reminder in reminders]]
            events = await uow.events.list_by_subjects(subjects)

            decisions = []
            results = []
            for action in actions:
                decisions.extend(await uow.actions.list_policy_decisions(action.id))
                result = await uow.actions.get_result(action.id)
                if result is not None:
                    results.append(result)

        history: list[TimelineEntry] = []
        for run in runs:
            history.append(
                TimelineEntry(
                    at=run.started_at.isoformat(),
                    kind="agent_run",
                    reference=run.id,
                    summary=f"attempt {run.attempt} by {run.agent_id} ({run.status.value})",
                    data={
                        "status": run.status.value,
                        "attempt": run.attempt,
                        "context_pack_hash": run.context_pack_hash,
                        "error_code": run.error_code.value if run.error_code else None,
                    },
                )
            )
        for action in actions:
            history.append(
                TimelineEntry(
                    at=action.created_at.isoformat(),
                    kind="action_request",
                    reference=action.id,
                    summary=f"{action.capability} ({action.status.value})",
                    data={
                        "capability": action.capability,
                        "payload_hash": action.payload_hash,
                        "idempotency_key": action.idempotency_key,
                        "status": action.status.value,
                    },
                )
            )
        for decision in decisions:
            history.append(
                TimelineEntry(
                    at=decision.created_at.isoformat(),
                    kind="policy_decision",
                    reference=decision.id,
                    summary=(
                        f"{decision.decision.value} "
                        f"(cedar={decision.cedar_decision.value}, {decision.reason_code})"
                    ),
                    data={
                        "action_request_id": decision.action_request_id,
                        "policy_ids": decision.determining_policy_ids,
                        "cedar_version": decision.cedar_version,
                    },
                )
            )
        for result in results:
            history.append(
                TimelineEntry(
                    at=result.completed_at.isoformat(),
                    kind="action_result",
                    reference=result.id,
                    summary=f"{result.status.value} ({result.external_ref})",
                    data={"action_request_id": result.action_request_id, **result.result},
                )
            )
        for reminder in reminders:
            history.append(
                TimelineEntry(
                    at=reminder.created_at.isoformat(),
                    kind="reminder",
                    reference=reminder.id,
                    summary=f"{reminder.title} ({reminder.status.value})",
                    data={
                        "scheduled_for_utc": reminder.scheduled_for_utc.isoformat(),
                        "timezone": reminder.timezone,
                        "status": reminder.status.value,
                        "delivered_at": (
                            reminder.delivered_at.isoformat() if reminder.delivered_at else None
                        ),
                    },
                )
            )
        for notification in notifications:
            history.append(
                TimelineEntry(
                    at=notification.created_at.isoformat(),
                    kind="notification",
                    reference=notification.id,
                    summary=f"{notification.kind.value} ({notification.delivery_status.value})",
                    data={
                        "delivery_key": notification.delivery_key,
                        "delivery_status": notification.delivery_status.value,
                        "attempt_count": notification.attempt_count,
                    },
                )
            )
        for event in events:
            history.append(
                TimelineEntry(
                    at=event.time.isoformat(),
                    kind="event",
                    reference=event.id,
                    summary=event.type.value,
                    data={"subject": event.subject, "actor": event.actor, **event.data},
                )
            )

        history.sort(key=lambda entry: (entry.at, entry.kind, entry.reference))

        return TaskTimeline(
            task_id=task_id,
            current_state={
                "task_id": task.id,
                "intent_id": task.intent_id,
                "status": task.status.value,
                "execution_mode": task.execution_mode.value,
                "assigned_agent_id": task.assigned_agent_id,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "last_error_code": task.last_error_code.value if task.last_error_code else None,
                "updated_at": task.updated_at.isoformat(),
            },
            history=history,
            record_counts={
                "agent_runs": len(runs),
                "action_requests": len(actions),
                "policy_decisions": len(decisions),
                "action_results": len(results),
                "reminders": len(reminders),
                "notifications": len(notifications),
                "events": len(events),
            },
        )
