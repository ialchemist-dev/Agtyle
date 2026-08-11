"""The seam that lets a real LLM runtime replace the deterministic one without kernel change."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agtyle.domain.agents import AgentAssignment, AgentOutput, ContextPack


@runtime_checkable
class AgentRuntimePort(Protocol):
    runtime_name: str

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> AgentOutput:
        """Return structured, untrusted output for one assignment.

        Implementations must never call a Capability Adapter, resolve a secret, or
        mutate durable state. They propose; the kernel decides.
        """
        ...
