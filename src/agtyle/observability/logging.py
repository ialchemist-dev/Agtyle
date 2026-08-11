"""Structured JSON logging that carries domain identifiers and redacts secrets."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from agtyle.domain.common import redact_secrets

_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("agtyle_log_context", default=None)

DOMAIN_KEYS = (
    "intent_id",
    "task_id",
    "agent_run_id",
    "action_request_id",
    "policy_decision_id",
    "reminder_id",
    "notification_id",
    "worker_id",
)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never emits a full protected payload or a secret."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        payload.update(_CONTEXT.get() or {})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception_type"] = getattr(record.exc_info[0], "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class LogContext:
    """Bind domain identifiers for the duration of a block."""

    def __init__(self, **fields: str | None) -> None:
        self._fields = {key: value for key, value in fields.items() if value is not None}
        self._token: Any = None

    def __enter__(self) -> LogContext:
        self._token = _CONTEXT.set({**(_CONTEXT.get() or {}), **self._fields})
        return self

    def __exit__(self, *_: object) -> None:
        _CONTEXT.reset(self._token)


def configure_logging(level: str = "INFO", *, log_format: str = "json") -> None:
    """Idempotently install the process log handler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
