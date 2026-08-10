"""Notification delivery port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agtyle.domain.common import DomainModel
from agtyle.domain.notifications import Notification


class DeliveryResult(DomainModel):
    delivered: bool
    external_ref: str | None = None
    duplicate_suppressed: bool = False
    detail: str | None = None


@runtime_checkable
class NotificationPort(Protocol):
    adapter_name: str

    async def deliver(self, notification: Notification) -> DeliveryResult:
        """Deliver one Notification. Adapters must accept and honour ``delivery_key``."""
        ...
