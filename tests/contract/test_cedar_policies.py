"""The Cedar policy matrix, run through the real pinned engine.

These tests never stub Cedar. If the pinned binary is absent the suite fails rather than
skipping, because "authorization was not actually tested" is exactly the failure mode the
specification exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agtyle.adapters.authorization.cedar_cli import (
    REASON_REQUEST_INVALID,
    CedarCliAuthorization,
    CedarEntity,
)
from agtyle.config import CEDAR_PINNED_VERSION
from agtyle.domain.actions import CedarDecision
from agtyle.ports.authorization import AuthorizationRequest

from .conftest import REPO_ROOT

POLICY_DIR = REPO_ROOT / "policies" / "cedar"
SCHEMA = POLICY_DIR / "agtyle.cedarschema"
POLICIES = POLICY_DIR / "base.cedar"
TESTS_JSON = POLICY_DIR / "tests.json"
CEDAR_BINARY = REPO_ROOT / ".tools" / "cedar" / CEDAR_PINNED_VERSION / "cedar"

CASES: list[dict[str, Any]] = json.loads(TESTS_JSON.read_text(encoding="utf-8"))


def case_id(case: dict[str, Any]) -> str:
    return str(case["agtyle"]["id"])


@pytest.fixture(scope="session")
def cedar() -> CedarCliAuthorization:
    assert CEDAR_BINARY.is_file(), (
        f"the pinned Cedar CLI is missing at {CEDAR_BINARY}. "
        "Run `make bootstrap` (or `python scripts/install_cedar.py`) before verifying."
    )
    return CedarCliAuthorization(
        binary=CEDAR_BINARY,
        schema=SCHEMA,
        policies=POLICIES,
        pinned_version=CEDAR_PINNED_VERSION,
    )


def _request_from_case(case: dict[str, Any]) -> AuthorizationRequest:
    request = case["request"]
    principal_type, principal_id = _parse_uid(request["principal"])
    resource_type, resource_id = _parse_uid(request["resource"])
    _, action_id = _parse_uid(request["action"])
    return AuthorizationRequest(
        principal_type=principal_type,
        principal_id=principal_id,
        action_id=action_id,
        resource_type=resource_type,
        resource_id=resource_id,
        context=request["context"],
    )


def _parse_uid(uid: str) -> tuple[str, str]:
    namespace_and_type, _, quoted = uid.partition('::"')
    entity_type = namespace_and_type.removeprefix("Agtyle::")
    return entity_type, quoted.rstrip('"')


def _entities_from_case(case: dict[str, Any]) -> list[CedarEntity]:
    return [
        CedarEntity(
            entity_type=item["uid"]["type"].removeprefix("Agtyle::"),
            entity_id=item["uid"]["id"],
            attributes=item["attrs"],
        )
        for item in case["entities"]
    ]


# --------------------------------------------------------------------------------------
# Configuration and validation
# --------------------------------------------------------------------------------------


def test_the_pinned_cedar_binary_is_installed_and_correct(
    cedar: CedarCliAuthorization,
) -> None:
    assert cedar.configuration_problem() is None
    assert cedar.engine_version() == CEDAR_PINNED_VERSION


async def test_cedar_schema_and_policy_set_validate(cedar: CedarCliAuthorization) -> None:
    report = await cedar.validate_policy_set()
    assert report.valid, report.errors
    assert report.engine_version == CEDAR_PINNED_VERSION
    assert "permit-steward-direct-reminder" in report.policy_ids


def test_every_policy_declares_a_stable_id(cedar: CedarCliAuthorization) -> None:
    text = POLICIES.read_text(encoding="utf-8")
    statements = text.count("permit (") + text.count("forbid (")
    assert statements == len(cedar.policy_ids()) == 3


# --------------------------------------------------------------------------------------
# P01-P10 through the Python adapter
# --------------------------------------------------------------------------------------


def test_the_matrix_covers_p01_through_p10() -> None:
    assert [case_id(case) for case in CASES] == [f"P{index:02d}" for index in range(1, 11)]


@pytest.mark.parametrize("case", CASES, ids=case_id)
async def test_policy_matrix_through_the_real_engine(
    cedar: CedarCliAuthorization, case: dict[str, Any]
) -> None:
    expected = case["agtyle"]["expected_outcome"]
    result = cedar._authorize_with_entities(
        _request_from_case(case), _entities_from_case(case), CEDAR_PINNED_VERSION
    )
    assert result.decision.value == expected, (
        f"{case_id(case)} expected {expected}, got {result.decision.value}: {result.errors}"
    )
    if expected == "allow":
        assert result.determining_policy_ids == case["reason"]
    if expected == "error":
        assert result.reason_code == REASON_REQUEST_INVALID
        assert result.errors


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["agtyle"]["cedar_evaluable"]], ids=case_id
)
def test_policy_matrix_through_the_cedar_cli_test_runner(case: dict[str, Any]) -> None:
    """Run the same data through Cedar's own `run-tests`, independent of our adapter."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump([case], handle)
        tests_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                str(CEDAR_BINARY),
                "run-tests",
                "--tests",
                str(tests_path),
                "--policies",
                str(POLICIES),
                "--schema",
                str(SCHEMA),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        tests_path.unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "0 failed" in completed.stdout


# --------------------------------------------------------------------------------------
# Fail-closed behaviour
# --------------------------------------------------------------------------------------


def _valid_request() -> AuthorizationRequest:
    return _request_from_case(CASES[0])


async def test_missing_binary_fails_closed(tmp_path: Path) -> None:
    adapter = CedarCliAuthorization(
        binary=tmp_path / "absent-cedar",
        schema=SCHEMA,
        policies=POLICIES,
        pinned_version=CEDAR_PINNED_VERSION,
    )
    result = await adapter.authorize(_valid_request())
    assert result.decision is CedarDecision.ERROR
    assert result.reason_code == "cedar_configuration_missing"

    report = await adapter.validate_policy_set()
    assert not report.valid


async def test_missing_policy_file_fails_closed(tmp_path: Path) -> None:
    adapter = CedarCliAuthorization(
        binary=CEDAR_BINARY,
        schema=SCHEMA,
        policies=tmp_path / "absent.cedar",
        pinned_version=CEDAR_PINNED_VERSION,
    )
    result = await adapter.authorize(_valid_request())
    assert result.decision is CedarDecision.ERROR


async def test_version_mismatch_fails_closed() -> None:
    adapter = CedarCliAuthorization(
        binary=CEDAR_BINARY,
        schema=SCHEMA,
        policies=POLICIES,
        pinned_version="0.0.1",
    )
    result = await adapter.authorize(_valid_request())
    assert result.decision is CedarDecision.ERROR
    assert result.reason_code == "cedar_version_mismatch"


async def test_timeout_fails_closed() -> None:
    adapter = CedarCliAuthorization(
        binary=CEDAR_BINARY,
        schema=SCHEMA,
        policies=POLICIES,
        pinned_version=CEDAR_PINNED_VERSION,
        timeout_seconds=0.000_001,
    )
    result = await adapter.authorize(_valid_request())
    assert result.decision is CedarDecision.ERROR
    assert result.reason_code in {"cedar_timeout", "cedar_version_mismatch"}


async def test_invalid_policy_set_is_reported_and_blocks_readiness(tmp_path: Path) -> None:
    broken = tmp_path / "broken.cedar"
    broken.write_text("permit (principal, action, resource) when { nonexistent.field };\n")
    adapter = CedarCliAuthorization(
        binary=CEDAR_BINARY,
        schema=SCHEMA,
        policies=broken,
        pinned_version=CEDAR_PINNED_VERSION,
    )
    report = await adapter.validate_policy_set()
    assert not report.valid
    assert report.errors


async def test_temporary_request_files_are_removed(cedar: CedarCliAuthorization) -> None:
    before = set(Path(tempfile.gettempdir()).glob("agtyle-cedar-*"))
    await cedar.authorize(_valid_request())
    after = set(Path(tempfile.gettempdir()).glob("agtyle-cedar-*"))
    assert after <= before


async def test_diagnostics_do_not_disclose_the_temporary_request_path(
    cedar: CedarCliAuthorization,
) -> None:
    unknown_action = _valid_request().model_copy(update={"action_id": "reminder.explode"})
    result = await cedar.authorize(unknown_action)
    assert result.decision is CedarDecision.ERROR
    joined = " ".join(result.errors)
    assert "agtyle-cedar-" not in joined
    assert "<request>" in joined
