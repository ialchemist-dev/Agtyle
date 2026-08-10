"""Secret store port seam. Agents receive opaque references, never material."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agtyle.domain.common import DomainModel


class SecretRef(DomainModel):
    """An opaque pointer. Its value must never appear in a ContextPack or an Event."""

    name: str
    version: str | None = None


@runtime_checkable
class SecretStorePort(Protocol):
    async def resolve(self, ref: SecretRef) -> str:
        """Return secret material to a Capability Adapter only."""
        ...
