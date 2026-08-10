"""The Agtyle command line: initialize, validate, run, inspect and demonstrate.

CLI commands are a Gateway adapter. They map application results and typed errors to
console output and exit codes, and never touch SQLAlchemy models directly.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from agtyle import __version__
from agtyle.config import Settings, get_settings
from agtyle.domain.common import AgtyleError
from agtyle.observability.logging import configure_logging

app = typer.Typer(
    name="agtyle",
    help="Agtyle: an auditable execution spine for delegated agent work.",
    no_args_is_help=True,
    add_completion=False,
)
task_app = typer.Typer(help="Inspect Tasks.", no_args_is_help=True)
reminder_app = typer.Typer(help="Inspect Reminders.", no_args_is_help=True)
demo_app = typer.Typer(help="Run local demonstrations.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(reminder_app, name="reminder")
app.add_typer(demo_app, name="demo")

EXIT_OK = 0
EXIT_ERROR = 1


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, log_format=settings.log_format)
    return settings


def emit(document: object) -> None:
    """Print one machine-readable JSON document to standard output."""
    typer.echo(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def fail(error: AgtyleError) -> None:
    """Render a typed application error and exit non-zero."""
    typer.echo(json.dumps({"error": error.to_public_dict()}, ensure_ascii=False, indent=2))
    raise typer.Exit(EXIT_ERROR)


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
    settings = _settings()
    emit(json.loads(settings.model_dump_json()))


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
