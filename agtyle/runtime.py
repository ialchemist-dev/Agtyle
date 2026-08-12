from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .gmail_client import GmailClient


@dataclass
class TaskRecord:
    task_id: str
    action: str
    status: str
    created_at: float
    updated_at: float
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT
                )
                """
            )

    def put(self, task: TaskRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, action, status, created_at, updated_at, payload, result, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    result=excluded.result,
                    error=excluded.error
                """,
                (
                    task.task_id,
                    task.action,
                    task.status,
                    task.created_at,
                    task.updated_at,
                    json.dumps(task.payload, ensure_ascii=False),
                    json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
                    task.error,
                ),
            )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return TaskRecord(
            task_id=row["task_id"],
            action=row["action"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=json.loads(row["payload"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
        )


class ApprovalGuard:
    """Require a recent explicit spoken approval before a send is accepted."""

    APPROVAL_PATTERNS = [
        r"\bsend it\b",
        r"\bsend that\b",
        r"\bgo ahead(?: and send(?: it| that)?)?\b",
        r"\byes[, ]+send(?: it| that)?\b",
        r"\blooks good[, ]+(?:please )?send(?: it| that)?\b",
        r"发送吧",
        r"发出去",
        r"可以发送",
        r"确认发送",
        r"就这样发",
    ]

    def __init__(self, max_age_seconds: float = 20.0) -> None:
        self.max_age_seconds = max_age_seconds
        self._last_transcript = ""
        self._last_transcript_at = 0.0

    def observe_user_transcript(self, transcript: str) -> None:
        self._last_transcript = transcript.strip()
        self._last_transcript_at = time.monotonic()

    def has_recent_explicit_approval(self) -> bool:
        if time.monotonic() - self._last_transcript_at > self.max_age_seconds:
            return False
        text = self._last_transcript.lower()
        return any(re.search(pattern, text, flags=re.I) for pattern in self.APPROVAL_PATTERNS)


class ExecutiveRuntime:
    def __init__(
        self,
        gmail: GmailClient,
        store: TaskStore,
        event_queue: asyncio.Queue[dict[str, Any]],
        approval_guard: ApprovalGuard,
    ) -> None:
        self.gmail = gmail
        self.store = store
        self.event_queue = event_queue
        self.approval_guard = approval_guard
        self._jobs: set[asyncio.Task] = set()

    async def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "task_status":
            task = self.store.get(str(payload.get("task_id", "")))
            return {"accepted": True, "task": asdict(task) if task else None}

        if action == "send_draft" and not self.approval_guard.has_recent_explicit_approval():
            return {
                "accepted": False,
                "reason": "No recent explicit spoken approval. Read the draft back and ask the user to say send it / 可以发送.",
            }

        task = TaskRecord(
            task_id=f"task_{uuid.uuid4().hex[:12]}",
            action=action,
            status="queued",
            created_at=time.time(),
            updated_at=time.time(),
            payload=payload,
        )
        self.store.put(task)
        job = asyncio.create_task(self._run(task), name=task.task_id)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return {"accepted": True, "task_id": task.task_id, "status": "queued", "action": action}

    async def _run(self, task: TaskRecord) -> None:
        task.status = "running"
        task.updated_at = time.time()
        self.store.put(task)
        await self.event_queue.put({"type": "task.started", "task_id": task.task_id, "action": task.action})
        try:
            result = await asyncio.to_thread(self._execute_sync, task.action, task.payload)
            task.status = "completed"
            task.result = result
            task.updated_at = time.time()
            self.store.put(task)
            await self.event_queue.put(
                {
                    "type": "task.completed",
                    "task_id": task.task_id,
                    "action": task.action,
                    "result": result,
                }
            )
        except Exception as exc:  # noqa: BLE001 - runtime boundary must report failures
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.updated_at = time.time()
            self.store.put(task)
            await self.event_queue.put(
                {
                    "type": "task.failed",
                    "task_id": task.task_id,
                    "action": task.action,
                    "error": task.error,
                }
            )

    def _execute_sync(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        actions: dict[str, Callable[[], dict[str, Any]]] = {
            "search_email": lambda: self.gmail.search(
                str(payload.get("query") or "newer_than:1d"),
                int(payload.get("max_results") or self.gmail.max_results),
            ),
            "read_email": lambda: self.gmail.read(self._required(payload, "message_id")),
            "create_reply_draft": lambda: self.gmail.create_reply_draft(
                self._required(payload, "message_id"), self._required(payload, "body")
            ),
            "send_draft": lambda: self.gmail.send_draft(self._required(payload, "draft_id")),
        }
        try:
            return actions[action]()
        except KeyError as exc:
            raise ValueError(f"Unsupported action: {action}") from exc

    @staticmethod
    def _required(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key, "")).strip()
        if not value:
            raise ValueError(f"Missing required field: {key}")
        return value
