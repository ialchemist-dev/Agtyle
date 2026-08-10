"""Filesystem-backed Agent and Capability registry.

Startup either produces a registry that is complete and self-consistent, or it fails. A missing
playbook, an unregistered capability request or a duplicate Agent version is a configuration
error, not something to discover on the Action path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.domain.agents import AgentManifest
from agtyle.domain.common import ConfigurationInvalidError, canonical_json

REQUIRED_AGENT_FILES: Final[tuple[str, ...]] = (
    "manifest.yaml",
    "responsibility.md",
    "personality.md",
    "playbook.md",
)

AGENT_MANIFEST_SCHEMA_ID: Final = (
    "https://agtyle.local/contracts/schemas/v1/agent-manifest.schema.json"
)

#: Types the kernel itself produces or consumes. A manifest may not invent new ones.
REGISTERED_ASSIGNMENT_TYPES: Final[frozenset[str]] = frozenset({"interaction", "reminder_create"})
REGISTERED_OUTPUT_TYPES: Final[frozenset[str]] = frozenset(
    {"task_proposal", "direct_response", "reminder_action_request"}
)


@dataclass(frozen=True)
class RegisteredAgent:
    """An Agent manifest plus the prose files that define how it behaves."""

    manifest: AgentManifest
    directory: Path
    manifest_hash: str
    responsibility: str
    personality: str
    playbook: str

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> int:
        return self.manifest.version


@dataclass(frozen=True)
class RegisteredCapability:
    """A capability the kernel knows how to authorize, validate and execute."""

    name: str
    version: int
    action_schema_id: str
    adapter_name: str
    approval_eligible: bool
    enabled: bool = True


#: The baseline capability set. `reminder.create` is not approval-eligible because it is a
#: reversible local action the user asked for directly, so a Cedar deny means deny.
BASELINE_CAPABILITIES: Final[tuple[RegisteredCapability, ...]] = (
    RegisteredCapability(
        name="reminder.create",
        version=1,
        action_schema_id=("https://agtyle.local/contracts/schemas/v1/reminder-create.schema.json"),
        adapter_name="local_reminders",
        approval_eligible=False,
    ),
)


class Registry:
    """The loaded, validated view of which Agents exist and what they may propose."""

    def __init__(
        self,
        agents: dict[tuple[str, int], RegisteredAgent],
        capabilities: dict[tuple[str, int], RegisteredCapability],
    ) -> None:
        self._agents = agents
        self._capabilities = capabilities

    # -- agents ----------------------------------------------------------------------

    @property
    def agents(self) -> list[RegisteredAgent]:
        return [self._agents[key] for key in sorted(self._agents)]

    def agent(self, agent_id: str, version: int | None = None) -> RegisteredAgent:
        if version is not None:
            try:
                return self._agents[(agent_id, version)]
            except KeyError as exc:
                raise ConfigurationInvalidError(f"unknown agent {agent_id}@{version}") from exc
        matches = [agent for (name, _), agent in self._agents.items() if name == agent_id]
        if not matches:
            raise ConfigurationInvalidError(f"unknown agent: {agent_id}")
        return max(matches, key=lambda agent: agent.version)

    def has_agent(self, agent_id: str) -> bool:
        return any(name == agent_id for name, _ in self._agents)

    def agent_for_assignment(self, assignment_type: str) -> RegisteredAgent | None:
        for agent in self.agents:
            if agent.manifest.accepts_type(assignment_type):
                return agent
        return None

    # -- capabilities ----------------------------------------------------------------

    @property
    def capabilities(self) -> list[RegisteredCapability]:
        return [self._capabilities[key] for key in sorted(self._capabilities)]

    def capability(self, name: str, version: int = 1) -> RegisteredCapability | None:
        capability = self._capabilities.get((name, version))
        if capability is None or not capability.enabled:
            return None
        return capability

    def is_capability_registered(self, name: str, version: int = 1) -> bool:
        return self.capability(name, version) is not None

    def agent_may_request(self, agent_id: str, capability: str) -> bool:
        """Manifest check. A true answer still guarantees nothing about Cedar's decision."""
        if not self.has_agent(agent_id):
            return False
        return self.agent(agent_id).manifest.may_request(capability)


def manifest_hash(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def load_registry(
    agents_dir: Path,
    *,
    schemas: JsonSchemaRegistry,
    capabilities: tuple[RegisteredCapability, ...] = BASELINE_CAPABILITIES,
) -> Registry:
    """Load and validate every Agent directory, failing closed on any inconsistency."""
    if not agents_dir.is_dir():
        raise ConfigurationInvalidError(f"missing agents directory: {agents_dir}")

    capability_index = {(item.name, item.version): item for item in capabilities}
    for capability in capabilities:
        if (
            schemas.schema_id_for(capability.name, capability.version)
            != capability.action_schema_id
        ):
            raise ConfigurationInvalidError(
                f"capability {capability.name}@{capability.version} references "
                f"{capability.action_schema_id}, which is not the committed schema"
            )

    loaded: dict[tuple[str, int], RegisteredAgent] = {}
    for directory in sorted(path for path in agents_dir.iterdir() if path.is_dir()):
        agent = _load_agent(directory, schemas=schemas)
        key = (agent.id, agent.version)
        if key in loaded:
            raise ConfigurationInvalidError(f"duplicate agent {agent.id}@{agent.version}")
        loaded[key] = agent

    if not loaded:
        raise ConfigurationInvalidError(f"no agents found under {agents_dir}")

    for agent in loaded.values():
        _validate_agent_types(agent)
        for requested in agent.manifest.capabilities.requested:
            if not any(name == requested for name, _ in capability_index):
                raise ConfigurationInvalidError(
                    f"agent {agent.id} requests unregistered capability {requested!r}"
                )

    return Registry(loaded, capability_index)


def _load_agent(directory: Path, *, schemas: JsonSchemaRegistry) -> RegisteredAgent:
    for filename in REQUIRED_AGENT_FILES:
        if not (directory / filename).is_file():
            raise ConfigurationInvalidError(
                f"agent {directory.name} is missing required file {filename}"
            )

    raw = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigurationInvalidError(f"agent {directory.name} manifest must be a mapping")

    schema_result = schemas.validate_document(AGENT_MANIFEST_SCHEMA_ID, raw)
    if not schema_result.valid:
        raise ConfigurationInvalidError(
            f"agent {directory.name} manifest failed schema validation: "
            f"{'; '.join(schema_result.errors)}"
        )

    try:
        manifest = AgentManifest(**raw)
    except Exception as exc:
        raise ConfigurationInvalidError(
            f"agent {directory.name} manifest is invalid: {exc}"
        ) from exc

    if manifest.id != directory.name:
        raise ConfigurationInvalidError(
            f"agent directory {directory.name!r} does not match manifest id {manifest.id!r}"
        )

    return RegisteredAgent(
        manifest=manifest,
        directory=directory,
        manifest_hash=manifest_hash(raw),
        responsibility=(directory / "responsibility.md").read_text(encoding="utf-8"),
        personality=(directory / "personality.md").read_text(encoding="utf-8"),
        playbook=(directory / "playbook.md").read_text(encoding="utf-8"),
    )


def _validate_agent_types(agent: RegisteredAgent) -> None:
    unknown_accepts = set(agent.manifest.accepts) - REGISTERED_ASSIGNMENT_TYPES
    if unknown_accepts:
        raise ConfigurationInvalidError(
            f"agent {agent.id} accepts unregistered types: {sorted(unknown_accepts)}"
        )
    unknown_produces = set(agent.manifest.produces) - REGISTERED_OUTPUT_TYPES
    if unknown_produces:
        raise ConfigurationInvalidError(
            f"agent {agent.id} produces unregistered types: {sorted(unknown_produces)}"
        )
