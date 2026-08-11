"""Every process role that accepts or executes work must refuse to start when it cannot work.

This suite is parametrized over the four roles in §8's topology table rather than over the
commands that happen to exist today. A fifth role wired without the gate fails here, which is
exactly the omission that let `agtyle api` start with a tampered manifest.

Readiness reporting a problem is not sufficient protection: a local-first deployment has no
load balancer to honour a 503, so an unready process would still be serving requests.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agtyle import cli
from agtyle.adapters.gateways.api import create_app
from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.bootstrap import build_container, check_startup, migrate
from agtyle.config import Settings, reset_settings_cache
from agtyle.domain.common import ConfigurationInvalidError

#: The roles §8 lists as writing state or accepting interactions.
CLI_ROLES = ("worker", "scheduler", "notifications")
OPERATOR_TOOLS = ("validate", "registry check", "task show", "reminder show", "recover")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A private copy of the agents and policies, so a test can tamper without touching the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    root = tmp_path / "workspace"
    root.mkdir()
    shutil.copytree(repo_root / "agents", root / "agents")
    shutil.copytree(repo_root / "policies", root / "policies")
    shutil.copytree(repo_root / "contracts", root / "contracts")
    return root


@pytest.fixture
def gated_settings(settings: Settings, workspace: Path) -> Settings:
    """A fully initialized, consistent system: migrated, synced, valid policies."""
    tuned = settings.with_overrides(
        agents_dir=workspace / "agents",
        contracts_dir=workspace / "contracts",
        cedar_schema=workspace / "policies" / "cedar" / "agtyle.cedarschema",
        cedar_policies=workspace / "policies" / "cedar" / "base.cedar",
    )
    migrate(tuned)
    container = build_container(tuned, configure_logs=False)
    try:
        import asyncio

        asyncio.run(container.registry_sync.sync())
    finally:
        container.dispose()
    return tuned


@pytest.fixture
def environment(gated_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Point the CLI's environment-driven settings at this test's workspace."""
    for key, value in {
        "AGTYLE_ENV": "test",
        "AGTYLE_DATA_DIR": str(gated_settings.data_dir),
        "AGTYLE_DATABASE_URL": gated_settings.database_url,
        "AGTYLE_AGENTS_DIR": str(gated_settings.agents_dir),
        "AGTYLE_CONTRACTS_DIR": str(gated_settings.contracts_dir),
        "AGTYLE_CEDAR_SCHEMA": str(gated_settings.cedar_schema),
        "AGTYLE_CEDAR_POLICIES": str(gated_settings.cedar_policies),
        "AGTYLE_CEDAR_BINARY": str(gated_settings.cedar_binary),
        "AGTYLE_NOTIFICATION_ADAPTER": "recording",
    }.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    yield gated_settings
    reset_settings_cache()


# --------------------------------------------------------------------------------------
# Ways the system can be unable to work
# --------------------------------------------------------------------------------------


def tamper_manifest(settings: Settings) -> None:
    """Edit a manifest without recording a new snapshot (§10.6)."""
    path = settings.agents_dir / "steward" / "manifest.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["display_name"] = "Tampered Agent"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def break_policies(settings: Settings) -> None:
    """Leave a policy set that does not validate against the schema (§15.5)."""
    settings.cedar_policies.write_text(
        "permit (principal, action, resource) when { nonexistent.field };\n", encoding="utf-8"
    )


def remove_cedar(settings: Settings) -> Settings:
    """Take the engine away entirely."""
    return settings.with_overrides(cedar_binary=settings.data_dir / "absent-cedar")


BREAKAGES: dict[str, Callable[[Settings], None]] = {
    "registry_snapshot_disagrees": tamper_manifest,
    "cedar_policies_invalid": break_policies,
}


# --------------------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("breakage", sorted(BREAKAGES), ids=sorted(BREAKAGES))
async def test_startup_check_reports_each_kind_of_breakage(
    gated_settings: Settings, breakage: str
) -> None:
    BREAKAGES[breakage](gated_settings)
    container = build_container(gated_settings, configure_logs=False)
    try:
        report = await check_startup(container)
        assert not report.ready
        assert report.problems
    finally:
        container.dispose()


async def test_a_healthy_system_passes_every_startup_check(gated_settings: Settings) -> None:
    container = build_container(gated_settings, configure_logs=False)
    try:
        report = await check_startup(container)
        assert report.ready, report.problems
        assert report.database_migrated
        assert report.registry_snapshot_synced
        assert report.registry_snapshot_consistent
        assert report.cedar_policies_valid
        assert report.cedar_version == "4.12.0"
    finally:
        container.dispose()


async def test_a_missing_cedar_binary_stops_startup(gated_settings: Settings) -> None:
    container = build_container(remove_cedar(gated_settings), configure_logs=False)
    try:
        report = await check_startup(container)
        assert not report.ready
        assert not report.cedar_binary_present
    finally:
        container.dispose()


async def test_an_unmigrated_database_stops_a_role_but_not_a_tool(
    settings: Settings, workspace: Path
) -> None:
    tuned = settings.with_overrides(
        agents_dir=workspace / "agents",
        contracts_dir=workspace / "contracts",
        cedar_schema=workspace / "policies" / "cedar" / "agtyle.cedarschema",
        cedar_policies=workspace / "policies" / "cedar" / "base.cedar",
    )
    container = build_container(tuned, configure_logs=False)
    try:
        assert not (await check_startup(container, require_database=True)).ready
        # Operator tooling must still be able to say what is wrong.
        assert (await check_startup(container, require_database=False)).ready
    finally:
        container.dispose()


# --------------------------------------------------------------------------------------
# Every role, not just the ones that were easy to remember
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", CLI_ROLES)
@pytest.mark.parametrize("breakage", sorted(BREAKAGES), ids=sorted(BREAKAGES))
def test_cli_role_refuses_to_start(environment: Settings, role: str, breakage: str) -> None:
    BREAKAGES[breakage](environment)
    result = CliRunner().invoke(cli.app, [role, "--once"])
    assert result.exit_code != 0, f"{role} started despite {breakage}"
    assert "AGT-SYSTEM-001" in result.output + str(result.stderr or "")


@pytest.mark.parametrize("breakage", sorted(BREAKAGES), ids=sorted(BREAKAGES))
def test_the_api_refuses_to_serve(environment: Settings, breakage: str) -> None:
    """The gate lives in the lifespan, so it fires for any ASGI host, not only `agtyle api`."""
    BREAKAGES[breakage](environment)
    container = build_container(
        environment,
        notification_adapters={"recording": RecordingNotificationAdapter()},
        configure_logs=False,
    )
    try:
        with pytest.raises(ConfigurationInvalidError), TestClient(create_app(container=container)):
            pass  # pragma: no cover - entering the client must raise
    finally:
        container.dispose()


@pytest.mark.parametrize("breakage", sorted(BREAKAGES), ids=sorted(BREAKAGES))
def test_the_api_never_accepts_work_while_broken(environment: Settings, breakage: str) -> None:
    """The original defect: readiness said 503 while the interaction endpoint still worked."""
    BREAKAGES[breakage](environment)
    container = build_container(
        environment,
        notification_adapters={"recording": RecordingNotificationAdapter()},
        configure_logs=False,
    )
    accepted = False
    try:
        with TestClient(create_app(container=container)) as client:
            response = client.post(
                "/v1/interactions",
                json={
                    "user_id": "user_local",
                    "conversation_id": "conv",
                    "channel": "api",
                    "input": "Remind me to slip past the gate at 2027-01-01T00:00:00Z",
                },
                headers={"Idempotency-Key": "gate-1"},
            )
            accepted = response.status_code == 202
    except ConfigurationInvalidError:
        accepted = False
    finally:
        container.dispose()

    assert not accepted, "a broken system accepted work"

    engine_container = build_container(environment, configure_logs=False)
    try:

        async def count() -> int:
            async with engine_container.uow_factory() as uow:
                return await uow.tasks.count_all()

        import asyncio

        assert asyncio.run(count()) == 0, "a broken system created a Task"
    finally:
        engine_container.dispose()


@pytest.mark.parametrize(
    "command",
    [["validate"], ["registry", "check"], ["recover"]],
    ids=lambda c: " ".join(c),
)
def test_operator_tools_still_work_on_a_broken_system(
    environment: Settings, command: list[str]
) -> None:
    """Diagnosis and repair must survive the failure they exist to diagnose and repair."""
    tamper_manifest(environment)
    result = CliRunner().invoke(cli.app, command)
    assert "Traceback" not in result.output
    if command == ["validate"]:
        assert result.exit_code != 0, "validate must report the problem"
        assert "registry_snapshot_consistent" in result.output
    elif command == ["registry", "check"]:
        assert result.exit_code != 0
        assert "manifest_hash_changed" in result.output
    else:
        assert result.exit_code == 0, "recover must still reclaim abandoned work"


def test_healthy_roles_still_start(environment: Settings) -> None:
    for role in CLI_ROLES:
        result = CliRunner().invoke(cli.app, [role, "--once"])
        assert result.exit_code == 0, f"{role} failed on a healthy system: {result.output}"


def test_every_long_running_role_is_covered_by_this_suite() -> None:
    """A new role added without a gate should fail this suite, not ship quietly.

    `CLI_ROLES` plus `api` must account for every command that runs a worker loop or serves
    traffic. If someone adds a fifth role, this list is where they are forced to notice.
    """
    import typer.main

    commands = set(typer.main.get_command(cli.app).commands)  # type: ignore[attr-defined]
    covered = set(CLI_ROLES) | {"api"}
    assert covered <= commands, f"a covered role vanished from the CLI: {covered - commands}"

    known_non_roles = {
        "config",
        "init",
        "validate",
        "recover",
        "task",
        "reminder",
        "demo",
        "registry",
    }
    unaccounted = commands - covered - known_non_roles
    assert not unaccounted, (
        f"new command(s) {sorted(unaccounted)} are neither a gated role nor a known operator "
        "tool. Decide which, and add them to CLI_ROLES or known_non_roles."
    )
