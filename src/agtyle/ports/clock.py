"""Time is an explicit dependency. Domain and application code never call ``datetime.now``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Wall-clock adapter used by real process roles."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Deterministic clock for tests and the end-to-end scenario."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._now = value.astimezone(UTC)
        return self._now
