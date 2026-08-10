"""Scripted Agent runtimes for exercising kernel behaviour on bad or hostile Agent output."""

from __future__ import annotations

from agtyle.domain.agents import (
    AgentAssignment,
    AgentOutput,
    ContextPack,
)


class ScriptedRuntime:
    """Returns whatever the test scripted, so the kernel's handling can be asserted."""

    runtime_name = "scripted"

    def __init__(self, output: AgentOutput) -> None:
        self._output = output
        self.calls: list[AgentAssignment] = []

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> AgentOutput:
        self.calls.append(assignment)
        return self._output


class RaisingRuntime:
    """Raises, so transient agent-runtime failures can be exercised."""

    runtime_name = "raising"

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> AgentOutput:
        self.calls += 1
        raise self._error


class SlowRuntime:
    """Sleeps before answering, so an interactive budget can actually expire."""

    runtime_name = "slow"

    def __init__(self, output: AgentOutput, delay_seconds: float) -> None:
        self._output = output
        self._delay = delay_seconds

    async def run(self, assignment: AgentAssignment, context: ContextPack) -> AgentOutput:
        import asyncio

        await asyncio.sleep(self._delay)
        return self._output
