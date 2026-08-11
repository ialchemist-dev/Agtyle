"""Durable workflow engine port seam. Temporal is optional and not a baseline dependency."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agtyle.domain.common import DomainModel, JsonMapping


class WorkflowStart(DomainModel):
    workflow_name: str
    workflow_id: str
    input: JsonMapping = Field(default_factory=dict)


class WorkflowHandle(DomainModel):
    workflow_id: str
    run_id: str


@runtime_checkable
class WorkflowEnginePort(Protocol):
    async def start(self, workflow: WorkflowStart) -> WorkflowHandle:
        """Start a durable workflow for long, cross-day waiting."""
        ...
