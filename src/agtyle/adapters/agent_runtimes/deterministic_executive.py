"""Deterministic Executive runtime.

Recognizes one exact fixture family and refuses everything else. It never invents a time: if the
user did not supply one, the answer is a clarification, not a guess.
"""

from __future__ import annotations

import re
from typing import Final

from agtyle.domain.agents import (
    AgentAssignment,
    ClarificationResponse,
    ContextPack,
    ExecutiveOutput,
    TaskProposal,
)
from agtyle.domain.reminders import MAX_TITLE_LENGTH
from agtyle.domain.tasks import ExecutionMode

#: The recognized shape: ``Remind me to <title> at <RFC 3339 timestamp>``.
REMINDER_PATTERN: Final = re.compile(
    r"^\s*remind\s+me\s+to\s+(?P<title>.+?)\s+at\s+(?P<when>\S+)\s*$",
    re.IGNORECASE | re.DOTALL,
)

#: An RFC 3339 instant with either a numeric offset or a literal Z.
RFC3339_PATTERN: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)

RECURRENCE_HINTS: Final[tuple[str, ...]] = (
    "every day",
    "every week",
    "every month",
    "daily",
    "weekly",
    "monthly",
    "each morning",
    "recurring",
)


class DeterministicExecutiveRuntime:
    """Routes an interaction to exactly one Task proposal, a clarification, or nothing else."""

    runtime_name = "deterministic_executive"
    agent_id = "executive"

    def __init__(self, *, default_timezone: str) -> None:
        self._default_timezone = default_timezone

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> ExecutiveOutput:
        if assignment.assignment_type != "interaction":
            return ClarificationResponse(
                message="The Executive Agent only handles interactions.",
                missing=["interaction"],
            )

        text = (assignment.input_text or "").strip()
        if not text:
            return ClarificationResponse(message="What would you like me to do?", missing=["input"])

        lowered = text.lower()
        for hint in RECURRENCE_HINTS:
            if hint in lowered:
                return ClarificationResponse(
                    message=(
                        "Recurring reminders are not supported yet. "
                        "Which single date and time should I use?"
                    ),
                    missing=["scheduled_for"],
                )

        match = REMINDER_PATTERN.match(text)
        if match is None:
            return ClarificationResponse(
                message=(
                    "I can create a one-time reminder. Please phrase it as "
                    "'Remind me to <what> at <RFC 3339 timestamp>'."
                ),
                missing=["title", "scheduled_for"],
            )

        title = match.group("title").strip()
        when = match.group("when").strip()

        if not title or len(title) > MAX_TITLE_LENGTH:
            return ClarificationResponse(
                message=(
                    "The reminder text must be between 1 and "
                    f"{MAX_TITLE_LENGTH} characters. What should it say?"
                ),
                missing=["title"],
            )

        if not RFC3339_PATTERN.match(when):
            # Refusing to interpret "tomorrow" is deliberate: a guessed instant is a wrong one.
            return ClarificationResponse(
                message=(
                    f"I could not read {when!r} as an exact time. "
                    "Please give an RFC 3339 timestamp, for example 2026-08-10T15:00:00-06:00."
                ),
                missing=["scheduled_for"],
            )

        timezone = str(context.user_preferences.get("timezone") or self._default_timezone)
        return TaskProposal(
            task_type="reminder_create",
            assigned_agent_id="steward",
            execution_mode=ExecutionMode.DELEGATED,
            objective="Create a reminder",
            payload={"title": title, "scheduled_for": when, "timezone": timezone},
        )
