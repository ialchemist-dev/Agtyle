"""Console notification adapter.

Prints one JSON line per delivery so a human watching the demo and a script parsing the output
see exactly the same facts. The record stores structured facts; the sentence is rendered here.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from agtyle.domain.notifications import Notification, NotificationKind
from agtyle.ports.notification import DeliveryResult


def render_message(notification: Notification) -> str:
    """One fixed English sentence per notification kind, derived from stored facts only."""
    payload = notification.payload
    match notification.kind:
        case NotificationKind.TASK_COMPLETED:
            reference = payload.get("result_ref")
            suffix = f" ({reference})" if reference else ""
            return f"Task {payload.get('task_id')} completed: reminder created{suffix}."
        case NotificationKind.TASK_FAILED:
            return f"Task {payload.get('task_id')} failed with {payload.get('error_code')}."
        case NotificationKind.APPROVAL_REQUIRED:
            return f"Task {payload.get('task_id')} is waiting for your approval."
        case NotificationKind.REMINDER_DUE:
            return f"Reminder: {payload.get('title')}"
    raise AssertionError(f"unhandled notification kind: {notification.kind}")  # pragma: no cover


class ConsoleNotificationAdapter:
    """``NotificationPort`` that writes to a stream. Delivery is at-least-once by nature."""

    adapter_name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    async def deliver(self, notification: Notification) -> DeliveryResult:
        line = {
            "notification_id": notification.id,
            "kind": notification.kind.value,
            "delivery_key": notification.delivery_key,
            "message": render_message(notification),
            "timestamp": notification.updated_at.isoformat(),
        }
        self._stream.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        self._stream.flush()
        return DeliveryResult(delivered=True, external_ref=notification.delivery_key)
