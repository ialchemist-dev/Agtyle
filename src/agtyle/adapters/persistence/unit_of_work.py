"""The Unit of Work: one SQLite write transaction spanning every repository.

Nothing an application service writes is visible to another process until ``commit`` returns.
That is what makes "acknowledgement follows persistence" enforceable rather than aspirational.
"""

from __future__ import annotations

from contextlib import suppress
from types import TracebackType
from typing import Self

from sqlalchemy import Connection, Engine

from agtyle.adapters.persistence.repositories import (
    SqlActionRepository,
    SqlAgentRunRepository,
    SqlApprovalRepository,
    SqlEventRepository,
    SqlIntentRepository,
    SqlNotificationRepository,
    SqlRegistryRepository,
    SqlReminderRepository,
    SqlTaskRepository,
)
from agtyle.ports.repositories import (
    ActionRepository,
    AgentRunRepository,
    ApprovalRepository,
    EventRepository,
    IntentRepository,
    NotificationRepository,
    RegistryRepository,
    ReminderRepository,
    TaskRepository,
    UnitOfWorkPort,
)


class SqliteUnitOfWork:
    """A single transaction. Reused instances are not supported; create one per operation."""

    # Declared so the class structurally satisfies UnitOfWorkPort before `__aenter__` runs.
    intents: IntentRepository
    tasks: TaskRepository
    agent_runs: AgentRunRepository
    actions: ActionRepository
    approvals: ApprovalRepository
    reminders: ReminderRepository
    notifications: NotificationRepository
    events: EventRepository
    registry: RegistryRepository

    def __init__(self, engine: Engine, *, immediate: bool = True) -> None:
        self._engine = engine
        self._immediate = immediate
        self._connection: Connection | None = None
        self._committed = False
        self._closed = False

    async def __aenter__(self) -> Self:
        if self._connection is not None:
            raise RuntimeError("this unit of work has already been entered")
        connection = self._engine.connect()
        # BEGIN IMMEDIATE takes the write lock now instead of on first write, so concurrent
        # claim transactions queue on the busy timeout rather than failing as a late upgrade.
        connection.exec_driver_sql("BEGIN IMMEDIATE" if self._immediate else "BEGIN")
        self._connection = connection
        self.intents = SqlIntentRepository(connection)
        self.tasks = SqlTaskRepository(connection)
        self.agent_runs = SqlAgentRunRepository(connection)
        self.actions = SqlActionRepository(connection)
        self.approvals = SqlApprovalRepository(connection)
        self.reminders = SqlReminderRepository(connection)
        self.notifications = SqlNotificationRepository(connection)
        self.events = SqlEventRepository(connection)
        self.registry = SqlRegistryRepository(connection)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if not self._committed:
                # An unhandled failure must leave no partial Task, Reminder or Notification.
                await self.rollback()
        finally:
            self._close()

    @property
    def connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("unit of work is not active")
        return self._connection

    @property
    def committed(self) -> bool:
        return self._committed

    async def commit(self) -> None:
        self.connection.exec_driver_sql("COMMIT")
        self._committed = True

    async def rollback(self) -> None:
        if self._connection is None or self._closed:
            return
        # A rollback failure must never mask the exception that caused the rollback.
        with suppress(Exception):
            self._connection.exec_driver_sql("ROLLBACK")

    def _close(self) -> None:
        if self._connection is not None and not self._closed:
            self._connection.close()
            self._closed = True


class SqliteUnitOfWorkFactory:
    """Callable factory so application services never see the engine."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self) -> UnitOfWorkPort:
        return SqliteUnitOfWork(self._engine)

    @property
    def engine(self) -> Engine:
        return self._engine
