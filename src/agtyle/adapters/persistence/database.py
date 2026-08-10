"""SQLite engine configuration.

Every connection must enable foreign keys, WAL journaling and a busy timeout before it is
used. Startup fails rather than proceeding with a database that cannot satisfy the contract,
because silently losing referential integrity would undermine every audit guarantee above it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.pool import StaticPool

from agtyle.config import Settings
from agtyle.domain.common import ConfigurationInvalidError

REQUIRED_PRAGMAS: Final[dict[str, str]] = {
    "foreign_keys": "ON",
    "busy_timeout": "5000",
    "synchronous": "NORMAL",
}

BUSY_TIMEOUT_MILLISECONDS: Final = 5_000


def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
    """Apply the connection contract to every pooled connection, including reconnects."""
    if not isinstance(dbapi_connection, sqlite3.Connection):  # pragma: no cover - defensive
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA synchronous = NORMAL")
        # WAL is a database-level setting; it is a no-op for in-memory databases.
        cursor.execute("PRAGMA journal_mode = WAL")
    finally:
        cursor.close()


def create_database_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """Build the process engine and verify that the connection contract actually took effect."""
    url = settings.database_url
    in_memory = url.endswith(":memory:") or "mode=memory" in url
    kwargs: dict[str, Any] = {
        "echo": echo,
        "future": True,
        "connect_args": {"timeout": BUSY_TIMEOUT_MILLISECONDS / 1000, "isolation_level": None},
    }
    if in_memory:
        # One shared connection so that an in-memory database survives between sessions.
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"]["check_same_thread"] = False
    else:
        database_path = Path(settings.database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _configure_connection)
    _verify_connection_contract(engine, require_wal=not in_memory)
    return engine


def _verify_connection_contract(engine: Engine, *, require_wal: bool) -> None:
    with engine.connect() as connection:
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        if int(foreign_keys) != 1:
            raise ConfigurationInvalidError(
                "SQLite refused to enable foreign key enforcement; refusing to start"
            )
        busy_timeout = int(connection.execute(text("PRAGMA busy_timeout")).scalar_one())
        if busy_timeout < BUSY_TIMEOUT_MILLISECONDS:
            raise ConfigurationInvalidError(
                f"SQLite busy_timeout is {busy_timeout}ms, expected at least "
                f"{BUSY_TIMEOUT_MILLISECONDS}ms"
            )
        if require_wal:
            journal_mode = str(connection.execute(text("PRAGMA journal_mode")).scalar_one())
            if journal_mode.lower() != "wal":
                raise ConfigurationInvalidError(
                    f"SQLite journal_mode is {journal_mode!r}, expected 'wal'"
                )
