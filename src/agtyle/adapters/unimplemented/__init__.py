"""Explicit `NotImplementedAdapter` implementations for seams outside the reminder baseline.

These exist so the ports are real, contract-tested and wired, while making it impossible for a
missing production adapter to look like success. Every method raises a typed configuration
error naming the port and the slice that will implement it.
"""

from __future__ import annotations

from typing import Final

from agtyle.domain.common import ConfigurationInvalidError
from agtyle.ports.knowledge import KnowledgeFragment, KnowledgeQuery
from agtyle.ports.secret_store import SecretRef
from agtyle.ports.workflow import WorkflowHandle, WorkflowStart

NOT_IMPLEMENTED: Final = "not_implemented"


def _refuse(port: str, slice_name: str) -> ConfigurationInvalidError:
    return ConfigurationInvalidError(
        f"{port} has no production adapter in this deployment. It is planned for {slice_name}. "
        "A missing adapter is a typed failure, never a silent success."
    )


class NotImplementedKnowledgeAdapter:
    """Knowledge retrieval arrives with Slice D. Until then, asking for it must fail loudly."""

    adapter_name = NOT_IMPLEMENTED

    async def retrieve(self, query: KnowledgeQuery) -> list[KnowledgeFragment]:
        raise _refuse("KnowledgePort", "Slice D (background research and Knowledge proposal)")


class NotImplementedSecretStoreAdapter:
    """Secret resolution arrives with the first slice that calls an external provider."""

    adapter_name = NOT_IMPLEMENTED

    async def resolve(self, ref: SecretRef) -> str:
        raise _refuse("SecretStorePort", "Slice B (interactive email read)")


class NotImplementedWorkflowAdapter:
    """A durable workflow engine is optional and is not part of the default runtime."""

    adapter_name = NOT_IMPLEMENTED

    async def start(self, workflow: WorkflowStart) -> WorkflowHandle:
        raise _refuse("WorkflowEnginePort", "an optional Temporal adapter")
