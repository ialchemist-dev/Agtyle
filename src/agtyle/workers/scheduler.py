"""Scheduler: turn due Reminders into due Notifications, exactly once each."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from agtyle.application.reminder_service import FiredReminder, ReminderService
from agtyle.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Scheduler:
    reminders: ReminderService
    poll_interval_seconds: float

    async def run_once(self) -> FiredReminder | None:
        fired = await self.reminders.claim_due()
        if fired is not None:
            logger.info(
                "reminder is due",
                extra={
                    "reminder_id": fired.reminder_id,
                    "notification_id": fired.notification_id,
                },
            )
        return fired

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                fired = await self.run_once()
            except Exception:
                logger.exception("scheduler iteration failed")
                fired = None
            if fired is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)
