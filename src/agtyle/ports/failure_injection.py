"""Failure injection seam.

Crash-safety cannot be argued, only demonstrated. Each named checkpoint marks a moment where a
process may die between two durable facts. Production wires the no-op implementation, so the
checkpoints cost one method call and change no behavior; tests wire an injector that raises at a
chosen checkpoint and then assert what survived.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class Checkpoint(StrEnum):
    """Every point where a crash must not lose or duplicate an effect."""

    AFTER_TASK_CLAIM_COMMIT = "after_task_claim_commit"
    AFTER_AGENT_OUTPUT = "after_agent_output"
    AFTER_ACTION_PERSISTED = "after_action_persisted"
    AFTER_POLICY_DECISION_COMMIT = "after_policy_decision_commit"
    AFTER_CAPABILITY_EXECUTION = "after_capability_execution"
    AFTER_COMPLETION_COMMIT = "after_completion_commit"
    AFTER_REMINDER_FIRING_COMMIT = "after_reminder_firing_commit"
    AFTER_ADAPTER_DELIVERY = "after_adapter_delivery"


@runtime_checkable
class FailureInjectorPort(Protocol):
    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        """Raise to simulate a process dying at this point, or return to continue."""
        ...


class NullFailureInjector:
    """The production implementation: every checkpoint is a no-op."""

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        return None
