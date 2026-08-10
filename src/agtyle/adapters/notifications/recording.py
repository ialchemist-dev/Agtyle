"""Recording notification adapter used for acceptance.

Exactly-once *delivery* cannot be guaranteed for an arbitrary external channel. What Agtyle can
guarantee is exactly-once Notification creation (unique delivery key) plus at-least-once adapter
invocation. This adapter closes the remaining gap locally by deduplicating on the delivery key,
so acceptance can assert exactly-once observed delivery.
"""

from __future__ import annotations

import threading

from agtyle.domain.notifications import Notification
from agtyle.ports.notification import DeliveryResult


class RecordingNotificationAdapter:
    """Test-only adapter. Production configuration refuses it outside AGTYLE_ENV=test."""

    adapter_name = "recording"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._delivered: dict[str, Notification] = {}
        self._invocations: list[str] = []
        self._fail_keys: set[str] = set()

    async def deliver(self, notification: Notification) -> DeliveryResult:
        with self._lock:
            self._invocations.append(notification.delivery_key)
            if notification.delivery_key in self._fail_keys:
                return DeliveryResult(delivered=False, detail="scripted delivery failure")
            if notification.delivery_key in self._delivered:
                # A retry after a crash reaches the adapter again; it must not duplicate.
                return DeliveryResult(
                    delivered=True,
                    external_ref=notification.delivery_key,
                    duplicate_suppressed=True,
                )
            self._delivered[notification.delivery_key] = notification
        return DeliveryResult(delivered=True, external_ref=notification.delivery_key)

    # -- test inspection -----------------------------------------------------------------

    @property
    def delivered_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._delivered)

    @property
    def invocations(self) -> list[str]:
        with self._lock:
            return list(self._invocations)

    def observed(self, delivery_key: str) -> Notification | None:
        with self._lock:
            return self._delivered.get(delivery_key)

    def fail_next(self, delivery_key: str) -> None:
        with self._lock:
            self._fail_keys.add(delivery_key)

    def stop_failing(self, delivery_key: str) -> None:
        with self._lock:
            self._fail_keys.discard(delivery_key)
