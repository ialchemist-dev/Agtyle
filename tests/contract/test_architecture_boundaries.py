"""Static proof that the dependency direction in AGENTS.md is actually respected.

This test reads the source tree rather than importing it, so a violation is reported as a
boundary failure instead of an import error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "agtyle"

FORBIDDEN_IN_DOMAIN = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "alembic",
    "subprocess",
    "requests",
    "httpx",
    "openai",
    "anthropic",
    "typer",
    "uvicorn",
    "pathlib",
    "os",
}

FORBIDDEN_IN_PORTS = FORBIDDEN_IN_DOMAIN - {"os", "pathlib"}

FORBIDDEN_IN_APPLICATION = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "alembic",
    "subprocess",
    "typer",
    "uvicorn",
}


def _module_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _module_files("domain"), ids=lambda p: p.name)
def test_domain_imports_no_infrastructure(path: Path) -> None:
    violations = _imported_roots(path) & FORBIDDEN_IN_DOMAIN
    assert not violations, f"{path.name} imports forbidden infrastructure: {sorted(violations)}"


@pytest.mark.parametrize("path", _module_files("ports"), ids=lambda p: p.name)
def test_ports_import_no_infrastructure(path: Path) -> None:
    violations = _imported_roots(path) & FORBIDDEN_IN_PORTS
    assert not violations, f"{path.name} imports forbidden infrastructure: {sorted(violations)}"


@pytest.mark.parametrize("path", _module_files("domain"), ids=lambda p: p.name)
def test_domain_does_not_import_application_or_adapters(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for forbidden in ("agtyle.application", "agtyle.adapters", "agtyle.workers", "agtyle.config"):
        assert forbidden not in text, f"{path.name} must not depend on {forbidden}"


def test_ports_depend_only_on_domain() -> None:
    for path in _module_files("ports"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("agtyle.application", "agtyle.adapters", "agtyle.workers"):
            assert forbidden not in text, f"{path.name} must not depend on {forbidden}"


def test_application_has_no_infrastructure_imports() -> None:
    for path in _module_files("application"):
        violations = _imported_roots(path) & FORBIDDEN_IN_APPLICATION
        assert not violations, f"{path.name} imports forbidden infrastructure: {sorted(violations)}"
        text = path.read_text(encoding="utf-8")
        assert "agtyle.adapters" not in text, f"{path.name} must not import an adapter"
