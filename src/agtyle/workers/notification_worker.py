"""Notification Worker: claim pending Notifications and deliver them."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from agtyle.application.notification_service import DeliveryReport, NotificationService
from agtyle.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class NotificationWorker:
    notifications: NotificationService
    poll_interval_seconds: float
    owner: str

    async def run_once(self) -> DeliveryReport | None:
        claimed = await self.notifications.claim_pending(owner=self.owner)
        if claimed is None:
            return None
        report = await self.notifications.deliver_claimed(claimed, owner=self.owner)
        logger.info(
            "notification attempt finished",
            extra={
                "notification_id": report.notification_id,
                "delivery_key": report.delivery_key,
                "outcome": report.outcome.value,
            },
        )
        return report

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("notification worker iteration failed")
                processed = None
            if processed is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)
