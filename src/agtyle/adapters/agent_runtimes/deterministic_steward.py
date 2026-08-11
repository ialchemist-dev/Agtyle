"""Deterministic Steward runtime.

Turns a validated reminder assignment into exactly one ActionRequest proposal. It supplies no
identifier, hash, idempotency key, task link or status: those are kernel-owned control values,
and an Agent trusted with them would effectively be authorizing itself.
"""

from __future__ import annotations

from datetime import datetime

from agtyle.domain.agents import (
    ActionRequestProposal,
    AgentAssignment,
    ClarificationResponse,
    ContextPack,
    SpecialistOutput,
)
from agtyle.domain.common import AgtyleError, to_storage
from agtyle.domain.reminders import (
    normalize_note,
    normalize_title,
    reject_recurrence,
    resolve_local_time,
    validate_timezone,
)

CAPABILITY = "reminder.create"
RESOURCE_TYPE = "ReminderCollection"


class DeterministicStewardRuntime:
    """Produces one `reminder.create` proposal, or escalates rather than guessing."""

    runtime_name = "deterministic_steward"
    agent_id = "steward"

    def __init__(self, *, default_timezone: str) -> None:
        self._default_timezone = default_timezone

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> SpecialistOutput:
        if assignment.assignment_type != "reminder_create":
            return ClarificationResponse(
                message="The Steward Agent only accepts reminder_create assignments.",
                missing=["assignment_type"],
            )

        if CAPABILITY not in context.authority_budget:
            # The authority budget is the Agent's own view of what it may propose.
            return ClarificationResponse(
                message="This assignment does not include authority to create a reminder.",
                missing=[CAPABILITY],
            )

        payload = dict(context.task.payload or assignment.payload)
        try:
            reject_recurrence(payload)
            title = normalize_title(str(payload.get("title", "")))
            note = normalize_note(payload.get("note"))
            timezone = str(
                payload.get("timezone")
                or context.user_preferences.get("timezone")
                or self._default_timezone
            )
            validate_timezone(timezone)
            instant = _resolve_instant(str(payload.get("scheduled_for", "")), timezone)
        except AgtyleError as error:
            return ClarificationResponse(message=error.detail, missing=["scheduled_for"])

        # The owning user is supplied by the kernel in the assignment, never inferred by the Agent.
        resource_id = str(assignment.payload.get("user_id", "user_local"))
        return ActionRequestProposal(
            capability=CAPABILITY,
            schema_version=1,
            resource_type=RESOURCE_TYPE,
            resource_id=resource_id,
            payload={
                "title": title,
                "note": note,
                "scheduled_for_utc": _rfc3339_utc(instant),
                "timezone": timezone,
            },
        )


def _resolve_instant(raw: str, timezone: str) -> datetime:
    """Convert what the user wrote into a UTC instant without ever guessing across a DST edge."""
    text = raw.strip()
    if not text:
        raise _clarify("The reminder needs a date and time.")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _clarify(f"{raw!r} is not a valid RFC 3339 timestamp.") from exc

    if parsed.tzinfo is not None:
        return parsed.astimezone(validate_timezone("UTC"))
    # A bare local time is interpreted in the reminder's zone, which may be ambiguous or absent.
    return resolve_local_time(local_naive=parsed, timezone_name=timezone)


def _rfc3339_utc(instant: datetime) -> str:
    """Render an instant in the one form the reminder schema accepts: UTC with a literal Z."""
    stamp = to_storage(instant)
    return stamp.replace(".000000Z", "Z") if stamp.endswith(".000000Z") else stamp


def _clarify(message: str) -> AgtyleError:
    from agtyle.domain.common import ClarificationRequiredError

    return ClarificationRequiredError(message)
