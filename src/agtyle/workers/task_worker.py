"""Task Worker: claim assigned Tasks, run their Agent, and finalize the attempt."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass

from agtyle.application.execution_service import (
    ClaimedTask,
    ExecutionReport,
    ExecutionService,
)
from agtyle.application.recovery_service import RecoveryService
from agtyle.domain.common import LeaseLostError
from agtyle.observability.logging import get_logger

logger = get_logger(__name__)


def worker_identity(role: str) -> str:
    """A stable, human-readable owner string so a stuck lease can be traced to a process."""
    return f"{role}@{socket.gethostname()}:{os.getpid()}"


@dataclass
class TaskWorker:
    """One polling loop. All correctness lives in ExecutionService, not here."""

    execution: ExecutionService
    recovery: RecoveryService
    poll_interval_seconds: float
    lease_seconds: int
    owner: str

    async def run_once(self) -> ExecutionReport | None:
        """Process at most one Task. Returns ``None`` when there was nothing to do."""
        await self.recovery.recover_expired_leases()
        claimed = await self.execution.claim_task(owner=self.owner)
        if claimed is None:
            return None

        heartbeat = asyncio.create_task(self._heartbeat(claimed))
        try:
            report = await self.execution.execute_claimed_task(claimed, owner=self.owner)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        logger.info(
            "task attempt finished",
            extra={
                "task_id": report.task_id,
                "outcome": report.outcome.value,
                "action_request_id": report.action_request_id,
            },
        )
        return report

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("task worker iteration failed")
                processed = None
            if processed is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)

    #: Renewals happen at a third of the lease, so the first one lands strictly before half of
    #: it has elapsed even if a renewal is briefly delayed. Renewing exactly at half would meet
    #: the letter of the rule and lose the race on any hiccup.
    HEARTBEAT_FRACTION = 3.0

    async def _heartbeat(self, claimed: ClaimedTask) -> None:
        """Renew the lease before half of it elapses, so a long Agent call keeps ownership."""
        interval = max(self.lease_seconds / self.HEARTBEAT_FRACTION, 0.05)
        task = claimed.task
        while True:
            await asyncio.sleep(interval)
            try:
                task = await self.execution.heartbeat(task, owner=self.owner)
            except LeaseLostError:
                logger.warning("lease lost during agent call", extra={"task_id": task.id})
                return
            except Exception:
                logger.exception("heartbeat failed", extra={"task_id": task.id})
                return
