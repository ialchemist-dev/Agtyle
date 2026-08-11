"""Tracing boundary.

The baseline ships a no-op span implementation so that an OpenTelemetry adapter can be
added later without touching application code.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TracerPort(Protocol):
    def span(self, name: str, **attributes: Any) -> Any:
        """Return a context manager representing one unit of work."""
        ...


class NoOpTracer:
    """Default tracer: correct, zero-dependency, and never a source of failure."""

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[None]:
        del name, attributes
        yield


_TRACER: TracerPort = NoOpTracer()


def get_tracer() -> TracerPort:
    return _TRACER


def set_tracer(tracer: TracerPort) -> None:
    global _TRACER
    _TRACER = tracer
