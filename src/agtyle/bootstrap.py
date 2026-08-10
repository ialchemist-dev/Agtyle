"""Dependency composition shared by every process role.

API, Task Worker, Scheduler and Notification Worker all build the same container from the same
modules, migrations and database. They are process roles inside one modular monolith, not
microservices, and this file is the single place that fact is expressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from agtyle.adapters.agent_runtimes.deterministic_executive import DeterministicExecutiveRuntime
from agtyle.adapters.agent_runtimes.deterministic_steward import DeterministicStewardRuntime
from agtyle.adapters.authorization.cedar_cli import CedarCliAuthorization
from agtyle.adapters.capabilities.local_reminders import LocalReminderCapability
from agtyle.adapters.notifications.console import ConsoleNotificationAdapter
from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
from agtyle.adapters.persistence import migrator
from agtyle.adapters.persistence.database import create_database_engine
from agtyle.adapters.persistence.unit_of_work import SqliteUnitOfWorkFactory
from agtyle.adapters.registry import Registry, load_registry
from agtyle.adapters.unimplemented import (
    NotImplementedKnowledgeAdapter,
    NotImplementedSecretStoreAdapter,
    NotImplementedWorkflowAdapter,
)
from agtyle.adapters.validation.json_schema import JsonSchemaRegistry
from agtyle.application.approval_service import ApprovalService
from agtyle.application.dispatch_service import DispatchService
from agtyle.application.execution_service import ExecutionService
from agtyle.application.interaction_service import InteractionService
from agtyle.application.notification_service import NotificationService
from agtyle.application.policy_service import PolicyService
from agtyle.application.recovery_service import RecoveryService
from agtyle.application.registry_sync import RegistrySyncService
from agtyle.application.reminder_service import ReminderService
from agtyle.application.retry_policy import RetryPolicy
from agtyle.application.timeline import TimelineService
from agtyle.config import (
    CEDAR_PINNED_VERSION,
    AgentRuntimeName,
    ConfigurationInvalidError,
    Environment,
    NotificationAdapterName,
    Settings,
    get_settings,
)
from agtyle.observability.logging import configure_logging
from agtyle.ports.agent_runtime import AgentRuntimePort
from agtyle.ports.capability import CapabilityPort
from agtyle.ports.clock import ClockPort, SystemClock
from agtyle.ports.failure_injection import FailureInjectorPort
from agtyle.ports.id_generator import IdGeneratorPort, Uuid7Generator
from agtyle.ports.knowledge import KnowledgePort
from agtyle.ports.notification import NotificationPort
from agtyle.ports.secret_store import SecretStorePort
from agtyle.ports.workflow import WorkflowEnginePort


@dataclass(frozen=True)
class Container:
    """Every dependency a process role needs, resolved once."""

    settings: Settings
    engine: Engine
    clock: ClockPort
    ids: IdGeneratorPort
    schemas: JsonSchemaRegistry
    registry: Registry
    authorization: CedarCliAuthorization
    uow_factory: SqliteUnitOfWorkFactory
    policy_service: PolicyService
    dispatch_service: DispatchService
    execution_service: ExecutionService
    interaction_service: InteractionService
    reminder_service: ReminderService
    notification_service: NotificationService
    recovery_service: RecoveryService
    timeline_service: TimelineService
    approval_service: ApprovalService
    registry_sync: RegistrySyncService
    knowledge: KnowledgePort
    secrets: SecretStorePort
    workflows: WorkflowEnginePort
    notification_adapters: dict[str, NotificationPort] = field(default_factory=dict)
    capabilities: dict[str, CapabilityPort] = field(default_factory=dict)
    agent_runtimes: dict[str, AgentRuntimePort] = field(default_factory=dict)

    def dispose(self) -> None:
        self.engine.dispose()


def build_container(
    settings: Settings | None = None,
    *,
    clock: ClockPort | None = None,
    ids: IdGeneratorPort | None = None,
    notification_adapters: dict[str, NotificationPort] | None = None,
    failures: FailureInjectorPort | None = None,
    configure_logs: bool = True,
) -> Container:
    """Compose the application. Any configuration problem raises rather than degrading."""
    resolved = settings or get_settings()
    if configure_logs:
        configure_logging(resolved.log_level, log_format=resolved.log_format)

    resolved.ensure_directories()
    _guard_adapter_selection(resolved, notification_adapters)

    engine = create_database_engine(resolved)
    uow_factory = SqliteUnitOfWorkFactory(engine)
    resolved_clock = clock or SystemClock()
    resolved_ids = ids or Uuid7Generator()

    schemas = JsonSchemaRegistry(resolved.contracts_dir)
    registry = load_registry(resolved.agents_dir, schemas=schemas)

    authorization = CedarCliAuthorization(
        binary=resolved.cedar_binary,
        schema=resolved.cedar_schema,
        policies=resolved.cedar_policies,
        pinned_version=CEDAR_PINNED_VERSION,
        timeout_seconds=resolved.cedar_timeout_seconds,
    )

    retry_policy = RetryPolicy(
        base_delay_seconds=resolved.retry_base_delay_seconds,
        max_delay_seconds=resolved.retry_max_delay_seconds,
        jitter_ratio=resolved.retry_jitter_ratio,
    )

    adapters = notification_adapters or _default_notification_adapters(resolved)
    capabilities: dict[str, CapabilityPort] = {
        LocalReminderCapability.capability_name: LocalReminderCapability(
            uow_factory=uow_factory, clock=resolved_clock, ids=resolved_ids
        )
    }
    runtimes: dict[str, AgentRuntimePort] = {
        "executive": DeterministicExecutiveRuntime(default_timezone=resolved.local_timezone),
        "steward": DeterministicStewardRuntime(default_timezone=resolved.local_timezone),
    }

    preferences: dict[str, Any] = {"timezone": resolved.local_timezone}

    policy_service = PolicyService(
        registry=registry,
        schemas=schemas,
        authorization=authorization,
        clock=resolved_clock,
        ids=resolved_ids,
    )
    dispatch_service = DispatchService(
        uow_factory=uow_factory, registry=registry, clock=resolved_clock, ids=resolved_ids
    )
    execution_service = ExecutionService(
        uow_factory=uow_factory,
        registry=registry,
        policy_service=policy_service,
        schemas=schemas,
        agent_runtimes=runtimes,
        capabilities=capabilities,
        clock=resolved_clock,
        ids=resolved_ids,
        retry_policy=retry_policy,
        lease_seconds=resolved.task_lease_seconds,
        notification_adapter=resolved.notification_adapter.value,
        max_notification_attempts=resolved.max_notification_attempts,
        user_preferences=preferences,
        failures=failures,
    )
    interaction_service = InteractionService(
        uow_factory=uow_factory,
        dispatch=dispatch_service,
        execution=execution_service,
        executive_runtime=runtimes["executive"],
        clock=resolved_clock,
        ids=resolved_ids,
        max_task_attempts=resolved.max_task_attempts,
        interactive_budget_seconds=resolved.interactive_budget_seconds,
        user_preferences=preferences,
    )
    reminder_service = ReminderService(
        uow_factory=uow_factory,
        clock=resolved_clock,
        ids=resolved_ids,
        lease_seconds=resolved.notification_lease_seconds,
        notification_adapter=resolved.notification_adapter.value,
        max_notification_attempts=resolved.max_notification_attempts,
        failures=failures,
    )
    notification_service = NotificationService(
        uow_factory=uow_factory,
        adapters=adapters,
        clock=resolved_clock,
        ids=resolved_ids,
        retry_policy=retry_policy,
        lease_seconds=resolved.notification_lease_seconds,
        failures=failures,
    )
    timeline_service = TimelineService(uow_factory=uow_factory)
    approval_service = ApprovalService(
        uow_factory=uow_factory, clock=resolved_clock, ids=resolved_ids
    )
    registry_sync = RegistrySyncService(
        uow_factory=uow_factory,
        registry=registry,
        manifest_hashes={
            (agent.id, agent.version): agent.manifest_hash for agent in registry.agents
        },
        capabilities=registry.capabilities,
        clock=resolved_clock,
    )
    recovery_service = RecoveryService(
        uow_factory=uow_factory,
        clock=resolved_clock,
        ids=resolved_ids,
        retry_policy=retry_policy,
        notification_adapter=resolved.notification_adapter.value,
        max_notification_attempts=resolved.max_notification_attempts,
    )

    return Container(
        settings=resolved,
        engine=engine,
        clock=resolved_clock,
        ids=resolved_ids,
        schemas=schemas,
        registry=registry,
        authorization=authorization,
        uow_factory=uow_factory,
        policy_service=policy_service,
        dispatch_service=dispatch_service,
        execution_service=execution_service,
        interaction_service=interaction_service,
        reminder_service=reminder_service,
        notification_service=notification_service,
        recovery_service=recovery_service,
        timeline_service=timeline_service,
        approval_service=approval_service,
        registry_sync=registry_sync,
        # Required seams with no production adapter in this baseline. They fail loudly rather
        # than quietly succeeding if the reminder path ever grows a dependency on them.
        knowledge=NotImplementedKnowledgeAdapter(),
        secrets=NotImplementedSecretStoreAdapter(),
        workflows=NotImplementedWorkflowAdapter(),
        notification_adapters=adapters,
        capabilities=capabilities,
        agent_runtimes=runtimes,
    )


def _default_notification_adapters(settings: Settings) -> dict[str, NotificationPort]:
    adapters: dict[str, NotificationPort] = {
        ConsoleNotificationAdapter.adapter_name: ConsoleNotificationAdapter()
    }
    if settings.test_adapters_permitted:
        adapters[RecordingNotificationAdapter.adapter_name] = RecordingNotificationAdapter()
    return adapters


def _guard_adapter_selection(
    settings: Settings, adapters: dict[str, NotificationPort] | None
) -> None:
    """Refuse to start production with a test double. There is no silent fallback."""
    if settings.env is not Environment.PRODUCTION:
        return
    problems: list[str] = []
    if settings.agent_runtime is AgentRuntimeName.DETERMINISTIC:
        problems.append("the deterministic agent runtime is a test adapter")
    if settings.notification_adapter is NotificationAdapterName.RECORDING:
        problems.append("the recording notification adapter is a test adapter")
    if adapters and RecordingNotificationAdapter.adapter_name in adapters:
        problems.append("a recording notification adapter was supplied to the container")
    if problems:
        raise ConfigurationInvalidError("production refuses test adapters: " + "; ".join(problems))


def migrate(settings: Settings) -> str:
    """Bring the database to head. Shared by `agtyle init`, tests and the demo."""
    return migrator.upgrade_to_head(settings)
