"""Registry port.

Application services need to know which Agents exist and which Capabilities are registered.
They must not know that this information happens to come from YAML files on disk.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agtyle.domain.agents import AgentManifest
from agtyle.domain.common import DomainModel


class CapabilitySpec(DomainModel):
    """A capability the kernel can authorize, validate and execute."""

    name: str
    version: int
    action_schema_id: str
    adapter_name: str
    approval_eligible: bool
    enabled: bool = True


@runtime_checkable
class AgentRegistryPort(Protocol):
    def has_agent(self, agent_id: str) -> bool: ...

    def agent_manifest(self, agent_id: str) -> AgentManifest:
        """Return the highest registered version of an Agent's manifest."""
        ...

    def agent_may_request(self, agent_id: str, capability: str) -> bool:
        """Manifest-level declaration only. It says nothing about what Cedar will decide."""
        ...

    def agent_for_assignment(self, assignment_type: str) -> AgentManifest | None: ...

    def capability(self, name: str, version: int = 1) -> CapabilitySpec | None: ...

    def is_capability_registered(self, name: str, version: int = 1) -> bool: ...
