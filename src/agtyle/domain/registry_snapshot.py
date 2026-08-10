"""Registry snapshots: the durable record of which Agents and Capabilities were in force.

The filesystem manifests and the code-level capability registration are the configured source
of truth. The database keeps a snapshot so that an execution recorded months ago can be
explained against the manifest that was actually enabled at the time, and so that an unnoticed
manifest edit cannot silently change what an Agent is allowed to propose.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from agtyle.domain.common import AgentId, CapabilityName, DomainModel, UtcDatetime


class AgentSnapshot(DomainModel):
    """One `(agent_id, version)` as it was when the registry was last synced."""

    agent_id: AgentId
    version: Annotated[int, Field(ge=1)]
    manifest_hash: str
    enabled: bool = True
    registered_at: UtcDatetime


class CapabilitySnapshot(DomainModel):
    """One `(name, version)` capability registration as it was when last synced."""

    name: CapabilityName
    version: Annotated[int, Field(ge=1)]
    action_schema_id: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    adapter_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    enabled: bool = True
    registered_at: UtcDatetime


class DisagreementKind(StrEnum):
    """Why a stored snapshot and the current configuration do not match."""

    MANIFEST_HASH_CHANGED = "manifest_hash_changed"
    AGENT_MISSING = "agent_missing"
    CAPABILITY_MISSING = "capability_missing"
    CAPABILITY_CHANGED = "capability_changed"


class Disagreement(DomainModel):
    kind: DisagreementKind
    subject: str
    detail: str


class RegistryConsistency(DomainModel):
    """The result of comparing the enabled snapshot against the current configuration."""

    synced: bool
    disagreements: list[Disagreement] = Field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.disagreements

    def describe(self) -> str:
        return "; ".join(
            f"{item.kind.value} for {item.subject}: {item.detail}" for item in self.disagreements
        )
