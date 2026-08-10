"""The Agtyle command line: initialize, validate, run, inspect and demonstrate.

The CLI is a Gateway adapter. It maps application results and typed errors to console output
and exit codes, and never touches SQLAlchemy models or a Capability Adapter directly.

Every `--once` command exits 0 when it successfully polls, including when there was no work.
A typed processing failure exits non-zero with a structured error document.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from agtyle import __version__
from agtyle.bootstrap import Container, build_container, migrate
from agtyle.config import Settings, get_settings
from agtyle.domain.common import AgtyleError, ConfigurationInvalidError
from agtyle.observability.logging import configure_logging
from agtyle.workers.notification_worker import NotificationWorker
from agtyle.workers.scheduler import Scheduler
from agtyle.workers.task_worker import TaskWorker, worker_identity

app = typer.Typer(
    name="agtyle",
    help="Agtyle: an auditable execution spine for delegated agent work.",
    no_args_is_help=True,
    add_completion=False,
)
task_app = typer.Typer(help="Inspect Tasks.", no_args_is_help=True)
reminder_app = typer.Typer(help="Inspect Reminders.", no_args_is_help=True)
demo_app = typer.Typer(help="Run local demonstrations.", no_args_is_help=True)
registry_app = typer.Typer(
    help="Inspect and synchronize the registry snapshot.", no_args_is_help=True
)
app.add_typer(task_app, name="task")
app.add_typer(reminder_app, name="reminder")
app.add_typer(demo_app, name="demo")
app.add_typer(registry_app, name="registry")

EXIT_OK = 0
EXIT_ERROR = 1


def emit(document: Any) -> None:
    """Print one machine-readable JSON document to standard output."""
    typer.echo(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def fail(error: AgtyleError) -> None:
    """Render a typed application error and exit non-zero."""
    typer.echo(
        json.dumps({"error": error.to_public_dict()}, ensure_ascii=False, indent=2), err=True
    )
    raise typer.Exit(EXIT_ERROR)


def load_settings(data_dir: Path | None = None, database_url: str | None = None) -> Settings:
    """Explicit CLI flags win over environment variables, `.env` and defaults."""
    settings = get_settings()
    if data_dir is not None or database_url is not None:
        settings = settings.with_overrides(data_dir=data_dir, database_url=database_url)
    configure_logging(settings.log_level, log_format=settings.log_format)
    return settings


def open_container(settings: Settings | None = None, *, check_registry: bool = True) -> Container:
    """Build the container and, for anything that will execute work, gate on the snapshot.

    A manifest edited without an explicit `agtyle registry sync` changes what an Agent may
    propose. That must stop the process, not be discovered later in an audit.
    """
    try:
        container = build_container(settings or load_settings())
    except AgtyleError as error:
        fail(error)
        raise  # pragma: no cover - fail always exits
    if check_registry:
        try:
            asyncio.run(container.registry_sync.require_consistent())
        except AgtyleError as error:
            container.dispose()
            fail(error)
    return container


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Print the Agtyle version and exit.")
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(EXIT_OK)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(EXIT_OK)


@app.command()
def config() -> None:
    """Print the effective, validated configuration."""
    emit(json.loads(load_settings().model_dump_json()))


@app.command()
def init() -> None:
    """Create data directories, run migrations, and validate the registry and policies."""
    settings = load_settings()
    settings.ensure_directories()
    head = migrate(settings)
    container = open_container(settings, check_registry=False)
    try:
        sync = asyncio.run(container.registry_sync.sync())
        report = asyncio.run(container.authorization.validate_policy_set())
        if not report.valid:
            fail(
                ConfigurationInvalidError(
                    "cedar policy validation failed: " + "; ".join(report.errors)
                )
            )
        emit(
            {
                "status": "initialized",
                "data_dir": str(settings.data_dir),
                "database_url": settings.database_url,
                "migration_head": head,
                "agents": [agent.id for agent in container.registry.agents],
                "capabilities": [item.name for item in container.registry.capabilities],
                "cedar_version": report.engine_version,
                "policy_ids": report.policy_ids,
                "registry_snapshot": sync.model_dump(mode="json"),
            }
        )
    finally:
        container.dispose()


@app.command()
def validate() -> None:
    """Validate configuration, registry, schemas, Cedar and the stored registry snapshot."""
    settings = load_settings()
    container = open_container(settings, check_registry=False)
    try:
        consistency = asyncio.run(container.registry_sync.check())
        report = asyncio.run(container.authorization.validate_policy_set())
        document = {
            "status": "valid" if report.valid else "invalid",
            "agents": [f"{agent.id}@{agent.version}" for agent in container.registry.agents],
            "capabilities": [
                f"{item.name}@{item.version}" for item in container.registry.capabilities
            ],
            "schemas": container.schemas.schema_ids,
            "cedar_version": report.engine_version,
            "cedar_valid": report.valid,
            "policy_ids": report.policy_ids,
            "errors": report.errors,
            "registry_snapshot_synced": consistency.synced,
            "registry_snapshot_consistent": consistency.consistent,
            "registry_disagreements": [
                item.model_dump(mode="json") for item in consistency.disagreements
            ],
        }
        emit(document)
        if not report.valid or not consistency.consistent:
            raise typer.Exit(EXIT_ERROR)
    finally:
        container.dispose()


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port. 0 selects a free port.")] = 8000,
) -> None:
    """Run the Interaction Gateway."""
    import uvicorn

    from agtyle.adapters.gateways.api import create_app

    settings = load_settings()
    uvicorn.run(create_app(settings), host=host, port=port, log_level=settings.log_level.lower())


@app.command()
def worker(
    once: Annotated[
        bool, typer.Option("--once", help="Process at most one Task and exit.")
    ] = False,
) -> None:
    """Run the Task Worker."""
    settings = load_settings()
    container = open_container(settings)
    owner = worker_identity("task-worker")
    runner = TaskWorker(
        execution=container.execution_service,
        recovery=container.recovery_service,
        poll_interval_seconds=settings.worker_poll_milliseconds / 1000,
        lease_seconds=settings.task_lease_seconds,
        owner=owner,
    )
    try:
        if once:
            report = asyncio.run(runner.run_once())
            emit(
                {
                    "polled": True,
                    "owner": owner,
                    "report": report.model_dump(mode="json") if report else None,
                }
            )
            return
        asyncio.run(runner.run_forever())
    except AgtyleError as error:
        fail(error)
    except KeyboardInterrupt:  # pragma: no cover - interactive use only
        typer.echo("stopped", err=True)
    finally:
        container.dispose()


@app.command()
def scheduler(
    once: Annotated[
        bool, typer.Option("--once", help="Fire at most one Reminder and exit.")
    ] = False,
) -> None:
    """Run the Reminder Scheduler."""
    settings = load_settings()
    container = open_container(settings)
    runner = Scheduler(
        reminders=container.reminder_service,
        poll_interval_seconds=settings.worker_poll_milliseconds / 1000,
    )
    try:
        if once:
            fired = asyncio.run(runner.run_once())
            emit({"polled": True, "fired": fired.model_dump(mode="json") if fired else None})
            return
        asyncio.run(runner.run_forever())
    except AgtyleError as error:
        fail(error)
    except KeyboardInterrupt:  # pragma: no cover - interactive use only
        typer.echo("stopped", err=True)
    finally:
        container.dispose()


@app.command()
def notifications(
    once: Annotated[
        bool, typer.Option("--once", help="Deliver at most one Notification and exit.")
    ] = False,
) -> None:
    """Run the Notification Worker."""
    settings = load_settings()
    container = open_container(settings)
    owner = worker_identity("notification-worker")
    runner = NotificationWorker(
        notifications=container.notification_service,
        poll_interval_seconds=settings.worker_poll_milliseconds / 1000,
        owner=owner,
    )
    try:
        if once:
            report = asyncio.run(runner.run_once())
            emit(
                {
                    "polled": True,
                    "owner": owner,
                    "report": report.model_dump(mode="json") if report else None,
                }
            )
            return
        asyncio.run(runner.run_forever())
    except AgtyleError as error:
        fail(error)
    except KeyboardInterrupt:  # pragma: no cover - interactive use only
        typer.echo("stopped", err=True)
    finally:
        container.dispose()


@app.command()
def recover() -> None:
    """Reclaim Tasks and Notifications abandoned by a crashed process."""
    container = open_container()
    try:
        summary = asyncio.run(container.recovery_service.recover_expired_leases())
        emit(summary.model_dump(mode="json"))
    finally:
        container.dispose()


# --------------------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------------------


@task_app.command("show")
def task_show(task_id: str) -> None:
    """Print the current state of one Task."""
    container = open_container()
    try:
        document = asyncio.run(_task_document(container, task_id))
        if document is None:
            fail(AgtyleError(f"no task with id {task_id}"))
        emit(document)
    finally:
        container.dispose()


@task_app.command("timeline")
def task_timeline(task_id: str) -> None:
    """Print the full explanation of one Task, current state and history clearly separated."""
    container = open_container()
    try:
        timeline = asyncio.run(container.timeline_service.for_task(task_id))
        if timeline is None:
            fail(AgtyleError(f"no task with id {task_id}"))
            return
        emit(timeline.model_dump(mode="json"))
    finally:
        container.dispose()


@reminder_app.command("show")
def reminder_show(reminder_id: str) -> None:
    """Print one Reminder and the Notifications attached to it."""
    container = open_container()
    try:
        document = asyncio.run(_reminder_document(container, reminder_id))
        if document is None:
            fail(AgtyleError(f"no reminder with id {reminder_id}"))
        emit(document)
    finally:
        container.dispose()


@registry_app.command("sync")
def registry_sync() -> None:
    """Record the current Agent and Capability registry as the enabled snapshot."""
    container = open_container(check_registry=False)
    try:
        emit(asyncio.run(container.registry_sync.sync()).model_dump(mode="json"))
    finally:
        container.dispose()


@registry_app.command("check")
def registry_check() -> None:
    """Report whether the stored snapshot still agrees with the configured registry."""
    container = open_container(check_registry=False)
    try:
        consistency = asyncio.run(container.registry_sync.check())
        emit(consistency.model_dump(mode="json"))
        if not consistency.consistent:
            raise typer.Exit(EXIT_ERROR)
    finally:
        container.dispose()


@demo_app.command("reminder")
def demo_reminder(
    due_in: Annotated[
        str, typer.Option("--due-in", help="Delay before the reminder is due.")
    ] = "5s",
) -> None:
    """Run the live multi-process reminder demonstration."""
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "reminder_demo.py"
    completed = subprocess.run([sys.executable, str(script), "--due-in", due_in], check=False)
    raise typer.Exit(completed.returncode)


async def _task_document(container: Container, task_id: str) -> dict[str, Any] | None:
    async with container.uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        if task is None:
            return None
        runs = await uow.agent_runs.list_for_task(task_id)
        actions = await uow.actions.list_for_task(task_id)
        reminders = await uow.reminders.list_for_task(task_id)
        notifications = await uow.notifications.list_for_task(task_id)
    return {
        "task": json.loads(task.model_dump_json()),
        "agent_runs": [json.loads(run.model_dump_json()) for run in runs],
        "action_requests": [json.loads(action.model_dump_json()) for action in actions],
        "reminders": [json.loads(item.model_dump_json()) for item in reminders],
        "notifications": [json.loads(item.model_dump_json()) for item in notifications],
    }


async def _reminder_document(container: Container, reminder_id: str) -> dict[str, Any] | None:
    async with container.uow_factory() as uow:
        reminder = await uow.reminders.get(reminder_id)
        if reminder is None:
            return None
        notifications = await uow.notifications.list_for_reminder(reminder_id)
    return {
        "reminder": json.loads(reminder.model_dump_json()),
        "local_time": reminder.local_time().isoformat(),
        "notifications": [json.loads(item.model_dump_json()) for item in notifications],
    }


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
