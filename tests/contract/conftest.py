"""Contract-test fixtures shared by schema, manifest and policy suites."""

from __future__ import annotations

from pathlib import Path

import pytest

from agtyle.adapters.validation.json_schema import JsonSchemaRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"
FIXTURES = CONTRACTS / "fixtures"


@pytest.fixture(scope="session")
def registry() -> JsonSchemaRegistry:
    return JsonSchemaRegistry(CONTRACTS)


def fixture_paths(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("*.json"))


def schema_name(path: Path) -> str:
    """Fixtures are named ``<schema-name>.<case>.json``; the first segment selects the schema."""
    return path.name.split(".")[0]
