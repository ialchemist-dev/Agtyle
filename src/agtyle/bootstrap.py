"""Dependency composition shared by every process role.

API, Task Worker, Scheduler and Notification Worker use this same container. They are
process roles inside one modular monolith, not microservices.
"""

from __future__ import annotations

from dataclasses import dataclass

from agtyle.config import Settings, get_settings
from agtyle.ports.clock import ClockPort, SystemClock
from agtyle.ports.id_generator import IdGeneratorPort, Uuid7Generator


@dataclass(frozen=True)
class Container:
    """Every dependency the application layer needs, resolved once per process."""

    settings: Settings
    clock: ClockPort
    ids: IdGeneratorPort


def build_container(
    settings: Settings | None = None,
    *,
    clock: ClockPort | None = None,
    ids: IdGeneratorPort | None = None,
) -> Container:
    resolved = settings or get_settings()
    return Container(
        settings=resolved,
        clock=clock or SystemClock(),
        ids=ids or Uuid7Generator(),
    )
