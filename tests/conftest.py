"""Shared deterministic test fixtures.

Every test runs against an isolated temporary data directory, a frozen clock and a
deterministic identifier generator so that failures are reproducible.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agtyle.config import Settings, reset_settings_cache
from agtyle.ports.clock import FrozenClock
from agtyle.ports.id_generator import DeterministicIdGenerator

REPO_ROOT = Path(__file__).resolve().parents[1]
E2E_NOW = datetime(2026, 8, 9, 22, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent a developer's own AGTYLE_* variables from leaking into a test run."""
    for key in list(os.environ):
        if key.startswith("AGTYLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGTYLE_ENV", "test")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(E2E_NOW)


@pytest.fixture
def ids() -> DeterministicIdGenerator:
    return DeterministicIdGenerator()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "agtyle-data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(
        env="test",
        data_dir=data_dir,
        database_url=f"sqlite:///{data_dir / 'agtyle.db'}",
        cedar_schema=REPO_ROOT / "policies" / "cedar" / "agtyle.cedarschema",
        cedar_policies=REPO_ROOT / "policies" / "cedar" / "base.cedar",
        cedar_binary=REPO_ROOT / ".tools" / "cedar" / "4.12.0" / "cedar",
        agents_dir=REPO_ROOT / "agents",
        contracts_dir=REPO_ROOT / "contracts",
        notification_adapter="recording",
        worker_poll_milliseconds=1,
    )
