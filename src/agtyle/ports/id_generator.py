"""Sortable, type-prefixed, never-reused identifiers.

The production generator emits UUIDv7 values so identifiers sort by creation time.
Tests inject a deterministic generator so that identifiers are reproducible.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from typing import Protocol, runtime_checkable

from agtyle.domain.common import IdPrefix


@runtime_checkable
class IdGeneratorPort(Protocol):
    def new_id(self, prefix: IdPrefix) -> str:
        """Return a unique identifier of the form ``<prefix>_<uuid>``."""
        ...


def uuid7() -> uuid.UUID:
    """Generate a UUID version 7 value (48-bit big-endian Unix milliseconds prefix)."""
    unix_ms = time.time_ns() // 1_000_000
    raw = bytearray(os.urandom(16))
    raw[0:6] = unix_ms.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


class Uuid7Generator:
    """Production identifier generator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ms = 0
        self._counter = 0

    def new_id(self, prefix: IdPrefix) -> str:
        return f"{prefix.value}_{self._next_uuid()}"

    def _next_uuid(self) -> uuid.UUID:
        """Monotonic within a process: identical milliseconds still sort by creation."""
        with self._lock:
            unix_ms = time.time_ns() // 1_000_000
            if unix_ms == self._last_ms:
                self._counter += 1
            else:
                self._last_ms = unix_ms
                self._counter = 0
            counter = self._counter
        raw = bytearray(16)
        raw[0:6] = unix_ms.to_bytes(6, "big")
        # 12 bits of monotonic counter in rand_a, then 62 bits of entropy in rand_b.
        raw[6] = 0x70 | ((counter >> 8) & 0x0F)
        raw[7] = counter & 0xFF
        tail = secrets.token_bytes(8)
        raw[8:16] = tail
        raw[8] = (raw[8] & 0x3F) | 0x80
        return uuid.UUID(bytes=bytes(raw))


class DeterministicIdGenerator:
    """Reproducible identifiers for tests: ``task_00000000-0000-7000-8000-000000000001``."""

    def __init__(self, seed: int = 0) -> None:
        self._counters: dict[str, int] = {}
        self._seed = seed
        self._lock = threading.Lock()

    def new_id(self, prefix: IdPrefix) -> str:
        with self._lock:
            index = self._counters.get(prefix.value, 0) + 1
            self._counters[prefix.value] = index
        value = uuid.UUID(
            fields=(
                self._seed,
                0,
                0x7000,
                0x80,
                0x00,
                index,
            )
        )
        return f"{prefix.value}_{value}"
