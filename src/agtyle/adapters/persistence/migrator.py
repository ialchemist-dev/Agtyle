"""Programmatic Alembic access so the CLI, tests and scripts share one migration path."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from agtyle.config import Settings

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
ALEMBIC_INI: Final = REPO_ROOT / "alembic.ini"
MIGRATIONS_DIR: Final = REPO_ROOT / "migrations"


def alembic_config(settings: Settings) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def head_revision(settings: Settings) -> str:
    script = ScriptDirectory.from_config(alembic_config(settings))
    revision = script.get_current_head()
    if revision is None:  # pragma: no cover - only possible with an empty versions directory
        raise RuntimeError("no Alembic head revision found")
    return revision


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade_to_head(settings: Settings) -> str:
    settings.ensure_directories()
    command.upgrade(alembic_config(settings), "head")
    return head_revision(settings)


def downgrade_to_base(settings: Settings) -> None:
    command.downgrade(alembic_config(settings), "base")


def is_up_to_date(settings: Settings, engine: Engine) -> bool:
    return current_revision(engine) == head_revision(settings)
