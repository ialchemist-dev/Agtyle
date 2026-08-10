"""A failure injector that raises at one chosen checkpoint, exactly once."""

from __future__ import annotations

from agtyle.ports.failure_injection import Checkpoint


class InjectedCrash(RuntimeError):
    """Stands in for a process dying between two durable facts."""


class CrashAt:
    """Raise at ``target`` the first ``times`` occasions it is reached, then stop."""

    def __init__(self, target: Checkpoint, *, times: int = 1) -> None:
        self.target = target
        self.remaining = times
        self.reached: list[Checkpoint] = []

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        self.reached.append(checkpoint)
        if checkpoint is self.target and self.remaining > 0:
            self.remaining -= 1
            raise InjectedCrash(f"simulated crash at {checkpoint.value}")

    @property
    def fired(self) -> bool:
        return self.remaining == 0
