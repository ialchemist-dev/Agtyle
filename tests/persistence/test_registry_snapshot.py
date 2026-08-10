"""Registry snapshots: recorded deliberately, compared at startup, never silently overwritten."""

from __future__ import annotations

import pytest

from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.adapters.registry import load_registry
from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.application.registry_sync import RegistrySyncService
from agtyle.config import Settings
from agtyle.domain.common import ConfigurationInvalidError
from agtyle.domain.registry_snapshot import DisagreementKind
from agtyle.ports.clock import FrozenClock
from agtyle.ports.registry import CapabilitySpec

from .conftest import NOW

REPO_ROOT = Settings(env="test").agents_dir.resolve().parent


@pytest.fixture
def schemas(settings: Settings) -> JsonSchemaRegistry:
    return JsonSchemaRegistry(settings.contracts_dir)


def build_service(
    settings: Settings,
    uow_factory: SqliteUnitOfWorkFactory,
    schemas: JsonSchemaRegistry,
    *,
    manifest_overrides: dict[tuple[str, int], str] | None = None,
    capabilities: list[CapabilitySpec] | None = None,
) -> RegistrySyncService:
    registry = load_registry(settings.agents_dir, schemas=schemas)
    hashes = {(agent.id, agent.version): agent.manifest_hash for agent in registry.agents}
    hashes.update(manifest_overrides or {})
    return RegistrySyncService(
        uow_factory=uow_factory,
        registry=registry,
        manifest_hashes=hashes,
        capabilities=capabilities if capabilities is not None else registry.capabilities,
        clock=FrozenClock(NOW),
    )


async def test_an_unsynced_database_is_not_a_disagreement(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    """A fresh database has simply never been synced; that must not block startup."""
    service = build_service(settings, uow_factory, schemas)
    consistency = await service.check()
    assert not consistency.synced
    assert consistency.consistent
    await service.require_consistent()


async def test_sync_records_every_agent_and_capability(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    service = build_service(settings, uow_factory, schemas)
    summary = await service.sync()
    assert summary.agents_recorded == 2
    assert summary.capabilities_recorded == 1

    async with uow_factory() as uow:
        agents = await uow.registry.list_agents()
        capabilities = await uow.registry.list_capabilities()
    assert {agent.agent_id for agent in agents} == {"executive", "steward"}
    assert all(agent.manifest_hash.startswith("sha256:") for agent in agents)
    assert [item.name for item in capabilities] == ["reminder.create"]
    assert all(item.enabled for item in agents + capabilities)  # type: ignore[operator]


async def test_sync_is_idempotent(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    service = build_service(settings, uow_factory, schemas)
    await service.sync()
    await service.sync()
    async with uow_factory() as uow:
        assert len(await uow.registry.list_agents()) == 2
        assert len(await uow.registry.list_capabilities()) == 1
    assert (await service.check()).consistent


async def test_a_changed_manifest_hash_blocks_startup(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    await build_service(settings, uow_factory, schemas).sync()

    edited = build_service(
        settings,
        uow_factory,
        schemas,
        manifest_overrides={("steward", 1): "sha256:" + "f" * 64},
    )
    consistency = await edited.check()
    assert not consistency.consistent
    assert consistency.disagreements[0].kind is DisagreementKind.MANIFEST_HASH_CHANGED
    assert consistency.disagreements[0].subject == "steward@1"

    with pytest.raises(ConfigurationInvalidError, match="registry sync"):
        await edited.require_consistent()


async def test_recording_the_new_version_unblocks_startup(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    await build_service(settings, uow_factory, schemas).sync()
    edited = build_service(
        settings,
        uow_factory,
        schemas,
        manifest_overrides={("steward", 1): "sha256:" + "f" * 64},
    )
    with pytest.raises(ConfigurationInvalidError):
        await edited.require_consistent()

    await edited.sync()
    assert (await edited.check()).consistent


async def test_a_removed_agent_is_reported_not_ignored(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    await build_service(settings, uow_factory, schemas).sync()

    service = build_service(settings, uow_factory, schemas)
    service._manifest_hashes.pop(("steward", 1))
    consistency = await service.check()
    assert not consistency.consistent
    assert consistency.disagreements[0].kind is DisagreementKind.AGENT_MISSING


async def test_a_changed_capability_registration_is_reported(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    await build_service(settings, uow_factory, schemas).sync()

    swapped = [
        CapabilitySpec(
            name="reminder.create",
            version=1,
            action_schema_id="https://agtyle.local/contracts/schemas/v1/reminder-create.schema.json",
            adapter_name="some_other_adapter",
            approval_eligible=False,
        )
    ]
    service = build_service(settings, uow_factory, schemas, capabilities=swapped)
    # The registry the service reads from still reports the real adapter, so the stored snapshot
    # disagrees with configuration once the adapter name changes.
    async with uow_factory() as uow:
        stored = await uow.registry.list_capabilities()
    assert stored[0].adapter_name == "local_reminders"
    await service.sync()
    async with uow_factory() as uow:
        rewritten = await uow.registry.list_capabilities()
    assert rewritten[0].adapter_name == "some_other_adapter"


async def test_snapshots_are_disabled_rather_than_deleted(
    settings: Settings, uow_factory: SqliteUnitOfWorkFactory, schemas: JsonSchemaRegistry
) -> None:
    """A removed Agent must still explain the AgentRuns it produced."""
    await build_service(settings, uow_factory, schemas).sync()

    service = build_service(settings, uow_factory, schemas)
    service._manifest_hashes.pop(("executive", 1))
    summary = await service.sync()
    assert summary.agents_disabled == 1

    async with uow_factory() as uow:
        agents = await uow.registry.list_agents()
    assert len(agents) == 2, "history is disabled, never deleted"
    disabled = [agent for agent in agents if not agent.enabled]
    assert [agent.agent_id for agent in disabled] == ["executive"]

    # A disabled snapshot no longer blocks startup.
    assert (await service.check()).consistent
