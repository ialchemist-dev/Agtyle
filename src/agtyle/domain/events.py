"""Event ledger: append-only records of important facts.

Events explain what happened. They never trigger execution and are never replayed to
reconstruct current state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from agtyle.domain.common import DomainModel, EventId, JsonMapping, UtcDatetime


class EventType(StrEnum):
    """Versioned event type names. Names are part of the durable audit contract."""

    INTENT_ACCEPTED = "agtyle.intent.accepted.v1"
    TASK_ASSIGNED = "agtyle.task.assigned.v1"
    TASK_STARTED = "agtyle.task.started.v1"
    TASK_COMPLETED = "agtyle.task.completed.v1"
    TASK_FAILED = "agtyle.task.failed.v1"
    TASK_CANCELLED = "agtyle.task.cancelled.v1"
    TASK_RECOVERED = "agtyle.task.recovered.v1"
    TASK_CONVERTED_TO_DELEGATED = "agtyle.task.converted_to_delegated.v1"
    AGENT_RUN_FAILED = "agtyle.agent_run.failed.v1"
    ACTION_PROPOSED = "agtyle.action.proposed.v1"
    ACTION_AUTHORIZED = "agtyle.action.authorized.v1"
    ACTION_DENIED = "agtyle.action.denied.v1"
    APPROVAL_REQUESTED = "agtyle.approval.requested.v1"
    APPROVAL_GRANTED = "agtyle.approval.granted.v1"
    APPROVAL_CONSUMED = "agtyle.approval.consumed.v1"
    REMINDER_CREATED = "agtyle.reminder.created.v1"
    REMINDER_FIRING = "agtyle.reminder.firing.v1"
    REMINDER_DELIVERED = "agtyle.reminder.delivered.v1"
    REMINDER_FAILED = "agtyle.reminder.failed.v1"
    NOTIFICATION_CREATED = "agtyle.notification.created.v1"
    NOTIFICATION_DELIVERED = "agtyle.notification.delivered.v1"
    NOTIFICATION_FAILED = "agtyle.notification.failed.v1"


class Event(DomainModel):
    """One immutable fact. The repository exposes append and query only."""

    id: EventId
    type: EventType
    subject: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    time: UtcDatetime
    data: JsonMapping = Field(default_factory=dict)


def system_actor() -> str:
    return "system:kernel"


def agent_actor(agent_id: str) -> str:
    return f"agent:{agent_id}"


def user_actor(user_id: str) -> str:
    return f"user:{user_id}"


def worker_actor(worker_id: str) -> str:
    return f"worker:{worker_id}"
