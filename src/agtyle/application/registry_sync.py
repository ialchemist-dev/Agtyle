"""Registry snapshot synchronization and the startup consistency gate.

A manifest is a security-relevant document: it declares which Capabilities an Agent may even
propose. Editing one silently would change the system's authority surface without any record.
So the database keeps a snapshot, startup compares against it, and a disagreement stops the
process until an operator records the new version deliberately with `agtyle registry sync`.

Snapshots are disabled, never deleted. A removed Agent still has to explain the AgentRuns it
produced last month.
"""

from __future__ import annotations

from agtyle.domain.common import ConfigurationInvalidError, DomainModel
from agtyle.domain.registry_snapshot import (
    AgentSnapshot,
    CapabilitySnapshot,
    Disagreement,
    DisagreementKind,
    RegistryConsistency,
)
from agtyle.observability.logging import get_logger
from agtyle.ports.clock import ClockPort
from agtyle.ports.registry import AgentRegistryPort, CapabilitySpec
from agtyle.ports.repositories import UnitOfWorkFactory

logger = get_logger(__name__)


class SyncSummary(DomainModel):
    agents_recorded: int
    capabilities_recorded: int
    agents_disabled: int
    capabilities_disabled: int


class RegistrySyncService:
    """Compares the configured registry with its durable snapshot, and records a new one."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        registry: AgentRegistryPort,
        manifest_hashes: dict[tuple[str, int], str],
        capabilities: list[CapabilitySpec],
        clock: ClockPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._manifest_hashes = manifest_hashes
        self._capabilities = capabilities
        self._clock = clock

    async def check(self) -> RegistryConsistency:
        """Compare enabled snapshot rows against the current configuration."""
        async with self._uow_factory() as uow:
            stored_agents = await uow.registry.list_agents()
            stored_capabilities = await uow.registry.list_capabilities()

        enabled_agents = [item for item in stored_agents if item.enabled]
        enabled_capabilities = [item for item in stored_capabilities if item.enabled]

        # An empty snapshot is not a disagreement: the database has simply never been synced.
        if not enabled_agents and not enabled_capabilities:
            return RegistryConsistency(synced=False)

        disagreements: list[Disagreement] = []

        for stored in enabled_agents:
            key = (stored.agent_id, stored.version)
            current_hash = self._manifest_hashes.get(key)
            if current_hash is None:
                disagreements.append(
                    Disagreement(
                        kind=DisagreementKind.AGENT_MISSING,
                        subject=f"{stored.agent_id}@{stored.version}",
                        detail="the snapshot is enabled but the agent is no longer configured",
                    )
                )
            elif current_hash != stored.manifest_hash:
                disagreements.append(
                    Disagreement(
                        kind=DisagreementKind.MANIFEST_HASH_CHANGED,
                        subject=f"{stored.agent_id}@{stored.version}",
                        detail=(
                            f"snapshot recorded {stored.manifest_hash[:23]}… "
                            f"but the manifest now hashes to {current_hash[:23]}…"
                        ),
                    )
                )

        for recorded in enabled_capabilities:
            current = self._registry.capability(recorded.name, recorded.version)
            if current is None:
                disagreements.append(
                    Disagreement(
                        kind=DisagreementKind.CAPABILITY_MISSING,
                        subject=f"{recorded.name}@{recorded.version}",
                        detail="the snapshot is enabled but the capability is not registered",
                    )
                )
            elif (
                current.action_schema_id != recorded.action_schema_id
                or current.adapter_name != recorded.adapter_name
            ):
                disagreements.append(
                    Disagreement(
                        kind=DisagreementKind.CAPABILITY_CHANGED,
                        subject=f"{recorded.name}@{recorded.version}",
                        detail=(
                            f"snapshot recorded schema {recorded.action_schema_id} via "
                            f"{recorded.adapter_name}; configuration now uses "
                            f"{current.action_schema_id} via {current.adapter_name}"
                        ),
                    )
                )

        return RegistryConsistency(synced=True, disagreements=disagreements)

    async def require_consistent(self) -> RegistryConsistency:
        """Startup gate. A disagreement stops the process rather than being logged and ignored."""
        consistency = await self.check()
        if not consistency.consistent:
            raise ConfigurationInvalidError(
                "the registry snapshot in the database disagrees with the configured registry: "
                f"{consistency.describe()}. Run `agtyle registry sync` to record the new version "
                "deliberately."
            )
        return consistency

    async def sync(self) -> SyncSummary:
        """Record the current registry as the enabled snapshot."""
        now = self._clock.now()
        agent_keys = sorted(self._manifest_hashes)
        capability_keys = [(item.name, item.version) for item in self._capabilities]

        async with self._uow_factory() as uow:
            for agent_id, version in agent_keys:
                await uow.registry.upsert_agent(
                    AgentSnapshot(
                        agent_id=agent_id,
                        version=version,
                        manifest_hash=self._manifest_hashes[(agent_id, version)],
                        enabled=True,
                        registered_at=now,
                    )
                )
            for capability in self._capabilities:
                await uow.registry.upsert_capability(
                    CapabilitySnapshot(
                        name=capability.name,
                        version=capability.version,
                        action_schema_id=capability.action_schema_id,
                        adapter_name=capability.adapter_name,
                        enabled=True,
                        registered_at=now,
                    )
                )
            agents_disabled = await uow.registry.disable_missing_agents(agent_keys)
            capabilities_disabled = await uow.registry.disable_missing_capabilities(capability_keys)
            await uow.commit()

        summary = SyncSummary(
            agents_recorded=len(agent_keys),
            capabilities_recorded=len(capability_keys),
            agents_disabled=agents_disabled,
            capabilities_disabled=capabilities_disabled,
        )
        logger.info("registry snapshot synced", extra=summary.model_dump())
        return summary
