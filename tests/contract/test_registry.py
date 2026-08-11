"""Registry contract: a valid registry starts, and every invalid one fails closed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agtyle.adapters.registry import (
    BASELINE_CAPABILITIES,
    RegisteredCapability,
    load_registry,
)
from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.application.context_builder import build_context_pack, context_pack_hash
from agtyle.domain.agents import TrustLabel
from agtyle.domain.common import ConfigurationInvalidError
from agtyle.domain.tasks import ExecutionMode, Task, TaskOrigin, TaskStatus

from .conftest import CONTRACTS, REPO_ROOT

AGENTS_DIR = REPO_ROOT / "agents"


@pytest.fixture
def agents_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "agents"
    shutil.copytree(AGENTS_DIR, destination)
    return destination


def _write_manifest(directory: Path, **changes: object) -> None:
    path = directory / "manifest.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_valid_registry_loads(registry: JsonSchemaRegistry) -> None:
    loaded = load_registry(AGENTS_DIR, schemas=registry)
    assert {agent.id for agent in loaded.agents} == {"executive", "steward"}
    assert loaded.is_capability_registered("reminder.create", 1)


def test_every_agent_manifest_validates(registry: JsonSchemaRegistry) -> None:
    loaded = load_registry(AGENTS_DIR, schemas=registry)
    for agent in loaded.agents:
        assert agent.manifest.version >= 1
        assert agent.manifest_hash.startswith("sha256:")
        assert agent.responsibility.strip()
        assert agent.personality.strip()
        assert agent.playbook.strip()


def test_steward_declares_the_specified_contract(registry: JsonSchemaRegistry) -> None:
    steward = load_registry(AGENTS_DIR, schemas=registry).agent("steward").manifest
    assert steward.accepts == ["reminder_create"]
    assert steward.produces == ["reminder_action_request"]
    assert steward.capabilities.requested == ["reminder.create"]
    assert set(steward.context_scopes) == {"timezone", "reminder_preferences"}
    assert steward.authority.external_publish.value == "forbidden"
    assert steward.escalation.ambiguity.value == "when_outcome_changes"


def test_executive_requests_no_external_capability(registry: JsonSchemaRegistry) -> None:
    executive = load_registry(AGENTS_DIR, schemas=registry).agent("executive").manifest
    assert executive.accepts == ["interaction"]
    assert set(executive.produces) == {"task_proposal", "direct_response"}
    assert executive.capabilities.requested == []


def test_every_registered_capability_has_a_schema_and_an_adapter(
    registry: JsonSchemaRegistry,
) -> None:
    loaded = load_registry(AGENTS_DIR, schemas=registry)
    for capability in loaded.capabilities:
        assert registry.schema_id_for(capability.name, capability.version) == (
            capability.action_schema_id
        )
        assert capability.adapter_name


def test_a_second_directory_claiming_the_same_agent_id_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry
) -> None:
    """Requiring directory name to equal manifest id makes duplicate ids structurally impossible."""
    shutil.copytree(agents_copy / "steward", agents_copy / "steward_copy")
    with pytest.raises(ConfigurationInvalidError, match="does not match manifest id"):
        load_registry(agents_copy, schemas=registry)


def test_manifest_failing_schema_validation_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry
) -> None:
    _write_manifest(agents_copy / "steward", version="one")
    with pytest.raises(ConfigurationInvalidError, match="schema validation"):
        load_registry(agents_copy, schemas=registry)


def test_unregistered_accepted_type_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry
) -> None:
    _write_manifest(agents_copy / "steward", accepts=["invent_something"])
    with pytest.raises(ConfigurationInvalidError, match="accepts unregistered types"):
        load_registry(agents_copy, schemas=registry)


def test_unregistered_produced_type_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry
) -> None:
    _write_manifest(agents_copy / "steward", produces=["email_draft"])
    with pytest.raises(ConfigurationInvalidError, match="produces unregistered types"):
        load_registry(agents_copy, schemas=registry)


def test_unregistered_requested_capability_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry
) -> None:
    _write_manifest(agents_copy / "steward", capabilities={"requested": ["finance.transfer"]})
    with pytest.raises(ConfigurationInvalidError, match="unregistered capability"):
        load_registry(agents_copy, schemas=registry)


@pytest.mark.parametrize("filename", ["responsibility.md", "personality.md", "playbook.md"])
def test_missing_prose_file_is_rejected(
    agents_copy: Path, registry: JsonSchemaRegistry, filename: str
) -> None:
    (agents_copy / "steward" / filename).unlink()
    with pytest.raises(ConfigurationInvalidError, match=filename):
        load_registry(agents_copy, schemas=registry)


def test_capability_referencing_an_unknown_schema_is_rejected(
    registry: JsonSchemaRegistry,
) -> None:
    broken = (
        RegisteredCapability(
            name="reminder.create",
            version=1,
            action_schema_id="https://agtyle.local/contracts/schemas/v1/does-not-exist.json",
            adapter_name="local_reminders",
            approval_eligible=False,
        ),
    )
    with pytest.raises(ConfigurationInvalidError, match="not the committed schema"):
        load_registry(AGENTS_DIR, schemas=registry, capabilities=broken)


def test_empty_agents_directory_is_rejected(tmp_path: Path, registry: JsonSchemaRegistry) -> None:
    empty = tmp_path / "no-agents"
    empty.mkdir()
    with pytest.raises(ConfigurationInvalidError, match="no agents found"):
        load_registry(empty, schemas=registry)


def test_baseline_capability_is_not_approval_eligible() -> None:
    reminder = next(item for item in BASELINE_CAPABILITIES if item.name == "reminder.create")
    assert reminder.approval_eligible is False


# --------------------------------------------------------------------------------------
# ContextPack
# --------------------------------------------------------------------------------------


def _task() -> Task:
    from datetime import UTC, datetime

    now = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
    return Task(
        id="task_00000000-0000-7000-8000-000000000001",
        intent_id="int_00000000-0000-7000-8000-000000000001",
        task_type="reminder_create",
        status=TaskStatus.RUNNING,
        execution_mode=ExecutionMode.DELEGATED,
        assigned_agent_id="steward",
        objective="Create a reminder",
        payload={"title": "submit the report"},
        origin=TaskOrigin(channel="api", conversation_id="conv_demo", user_id="user_local"),
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )


def test_context_pack_contains_only_allowed_scopes_and_no_secrets() -> None:
    pack = build_context_pack(
        task=_task(),
        context_scopes=["timezone", "reminder_preferences"],
        authority_budget=["reminder.create"],
        available_preferences={
            "timezone": "America/Denver",
            "reminder_lead_minutes": 10,
            "api_key": "sk-should-never-appear",
            "known_contacts": ["john"],
        },
        trust_labels={"interaction://conv_demo": TrustLabel.USER_DIRECT},
    )
    assert set(pack.user_preferences) == {"timezone", "reminder_lead_minutes"}
    serialized = pack.model_dump_json()
    assert "sk-should-never-appear" not in serialized
    assert "known_contacts" not in serialized
    assert pack.authority_budget == ["reminder.create"]


def test_context_pack_omits_preferences_for_undeclared_scopes() -> None:
    pack = build_context_pack(
        task=_task(),
        context_scopes=[],
        authority_budget=[],
        available_preferences={"timezone": "America/Denver"},
    )
    assert pack.user_preferences == {}


def test_context_pack_hash_is_stable_and_sensitive() -> None:
    first = build_context_pack(
        task=_task(),
        context_scopes=["timezone"],
        authority_budget=["reminder.create"],
        available_preferences={"timezone": "America/Denver"},
    )
    second = build_context_pack(
        task=_task(),
        context_scopes=["timezone"],
        authority_budget=["reminder.create"],
        available_preferences={"timezone": "America/Denver"},
    )
    different = build_context_pack(
        task=_task(),
        context_scopes=["timezone"],
        authority_budget=[],
        available_preferences={"timezone": "America/Denver"},
    )
    assert context_pack_hash(first) == context_pack_hash(second)
    assert context_pack_hash(first) != context_pack_hash(different)


def test_contracts_directory_is_the_one_under_test(registry: JsonSchemaRegistry) -> None:
    assert (CONTRACTS / "schemas" / "v1").is_dir()
    assert registry.schema_id_for("reminder.create", 1) is not None
