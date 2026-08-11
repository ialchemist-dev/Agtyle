"""Structured, validated configuration for every Agtyle process role.

Precedence is explicit CLI flags, then ``AGTYLE_`` environment variables, then a
local ``.env`` file, then defaults. Configuration is immutable once loaded.
"""

from __future__ import annotations

import zoneinfo
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agtyle.domain.common import ConfigurationInvalidError as _ConfigurationInvalidError

CEDAR_PINNED_VERSION = "4.12.0"
"""The only Cedar CLI version this implementation is verified against."""


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class NotificationAdapterName(StrEnum):
    CONSOLE = "console"
    RECORDING = "recording"


class AgentRuntimeName(StrEnum):
    DETERMINISTIC = "deterministic"


class ConfigurationError(RuntimeError):
    """Raised when configuration cannot satisfy the specification."""


#: Re-exported so composition code raises the same typed error the rest of the system uses.
ConfigurationInvalidError = _ConfigurationInvalidError


class Settings(BaseSettings):
    """Validated runtime configuration shared by API, Worker, Scheduler and Notifier."""

    model_config = SettingsConfigDict(
        env_prefix="AGTYLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = Environment.DEVELOPMENT
    data_dir: Path = Path(".agtyle")
    database_url: str = "sqlite:///.agtyle/agtyle.db"

    cedar_binary: Path = Path(".tools/cedar") / CEDAR_PINNED_VERSION / "cedar"
    cedar_schema: Path = Path("policies/cedar/agtyle.cedarschema")
    cedar_policies: Path = Path("policies/cedar/base.cedar")
    cedar_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0

    agents_dir: Path = Path("agents")
    contracts_dir: Path = Path("contracts")

    local_timezone: str = "America/Denver"
    task_lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 30
    notification_lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 30
    max_task_attempts: Annotated[int, Field(ge=1, le=100)] = 3
    max_notification_attempts: Annotated[int, Field(ge=1, le=100)] = 3
    worker_poll_milliseconds: Annotated[int, Field(ge=1, le=60_000)] = 250
    retry_base_delay_seconds: Annotated[float, Field(ge=0, le=3600)] = 1.0
    retry_max_delay_seconds: Annotated[float, Field(ge=0, le=86_400)] = 60.0
    retry_jitter_ratio: Annotated[float, Field(ge=0, le=1)] = 0.0
    interactive_budget_seconds: Annotated[float, Field(gt=0, le=300)] = 5.0

    agent_runtime: AgentRuntimeName = AgentRuntimeName.DETERMINISTIC
    notification_adapter: NotificationAdapterName = NotificationAdapterName.CONSOLE
    allow_test_adapters: bool = False

    log_level: str = "INFO"
    log_format: str = "json"

    @field_validator("local_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log level must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _guard_production_adapters(self) -> Self:
        """Production must never silently use deterministic or recording test adapters."""
        if self.env is not Environment.PRODUCTION:
            return self
        problems: list[str] = []
        if self.allow_test_adapters:
            problems.append("allow_test_adapters must be false when AGTYLE_ENV=production")
        if self.agent_runtime is AgentRuntimeName.DETERMINISTIC:
            problems.append(
                "production requires a real agent runtime; the deterministic runtime is "
                "a test-only adapter"
            )
        if self.notification_adapter is NotificationAdapterName.RECORDING:
            problems.append("the recording notification adapter is test-only")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def database_path(self) -> Path:
        """Filesystem path of the SQLite database, or ``:memory:`` for in-memory use."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ConfigurationError(
                "AGTYLE_DATABASE_URL must be a sqlite:/// URL in the local-first baseline"
            )
        return Path(self.database_url[len(prefix) :])

    @property
    def test_adapters_permitted(self) -> bool:
        return self.env is Environment.TEST or self.allow_test_adapters

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        database_path = self.database_path
        if str(database_path) != ":memory:":
            database_path.parent.mkdir(parents=True, exist_ok=True)

    def with_overrides(self, **overrides: Any) -> Settings:
        """Return a copy with explicit overrides, used for CLI flags and tests."""
        merged = self.model_dump()
        merged.update({key: value for key, value in overrides.items() if value is not None})
        return Settings(**merged)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings; used by tests that mutate the environment."""
    get_settings.cache_clear()
