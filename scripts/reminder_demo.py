#!/usr/bin/env python
"""Live multi-process reminder demonstration.

This is the proof that Agtyle works as a running system rather than as a test harness: four
real operating-system processes share one SQLite database, a reminder is created through the
real Cedar engine, both notification loops close on wall-clock time, and a restart produces no
duplicate.

Every wait is bounded. The script exits non-zero on timeout, a missing milestone, a duplicate
delivery, a leaked child process or a record-count mismatch, and it terminates every child in a
`finally` block on every path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

READINESS_TIMEOUT_SECONDS = 30.0
MILESTONE_TIMEOUT_SECONDS = 45.0
RESTART_OBSERVATION_SECONDS = 3.0
POLL_INTERVAL_SECONDS = 0.2
SHUTDOWN_GRACE_SECONDS = 5.0


class DemoFailure(RuntimeError):
    """A milestone was not reached, or something happened that must never happen."""


def passed(message: str) -> None:
    print(f"[PASS] {message}", flush=True)


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def failed(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr, flush=True)


@dataclass
class Child:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path

    def lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()


@dataclass
class Demo:
    workspace: Path
    port: int
    children: list[Child] = field(default_factory=list)
    #: Log files are per run, so a restart cannot truncate the previous run's evidence.
    run_index: int = 0

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.workspace / 'agtyle.db'}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "AGTYLE_ENV": "development",
                "AGTYLE_DATA_DIR": str(self.workspace),
                "AGTYLE_DATABASE_URL": self.database_url,
                "AGTYLE_NOTIFICATION_ADAPTER": "console",
                "AGTYLE_WORKER_POLL_MILLISECONDS": "100",
                "AGTYLE_TASK_LEASE_SECONDS": "15",
                "AGTYLE_NOTIFICATION_LEASE_SECONDS": "15",
                "AGTYLE_LOG_LEVEL": "WARNING",
                "PYTHONPATH": str(REPO_ROOT / "src"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    def run_cli(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, "-m", "agtyle.cli", *arguments],
            cwd=REPO_ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and completed.returncode != 0:
            raise DemoFailure(
                f"`agtyle {' '.join(arguments)}` exited {completed.returncode}: "
                f"{completed.stderr.strip()[:500]}"
            )
        return completed

    def start(self, name: str, *arguments: str) -> Child:
        log_path = self.workspace / f"{name}.run{self.run_index}.log"
        handle = log_path.open("wb")
        process = subprocess.Popen(
            [sys.executable, "-m", "agtyle.cli", *arguments],
            cwd=REPO_ROOT,
            env=self.environment(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        child = Child(name=name, process=process, log_path=log_path)
        self.children.append(child)
        info(f"started {name} (pid {process.pid})")
        return child

    def start_all_roles(self) -> list[Child]:
        self.run_index += 1
        return [
            self.start("api", "api", "--host", "127.0.0.1", "--port", str(self.port)),
            self.start("task-worker", "worker"),
            self.start("scheduler", "scheduler"),
            self.start("notification-worker", "notifications"),
        ]

    def stop_all(self) -> None:
        """Terminate every child, escalating to SIGKILL, and prove none survived."""
        for child in reversed(self.children):
            if child.process.poll() is not None:
                continue
            with suppress(ProcessLookupError):
                os.killpg(os.getpgid(child.process.pid), signal.SIGTERM)
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for child in reversed(self.children):
            remaining = max(deadline - time.monotonic(), 0.1)
            with suppress(subprocess.TimeoutExpired):
                child.process.wait(timeout=remaining)
            if child.process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(os.getpgid(child.process.pid), signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    child.process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        leaked = [child.name for child in self.children if child.process.poll() is None]
        if leaked:
            raise DemoFailure(f"child processes did not terminate: {leaked}")
        self.children.clear()

    # -- HTTP -------------------------------------------------------------------------

    def get(self, path: str) -> tuple[int, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            return 0, {"error": str(error)}

    def post(self, path: str, body: dict[str, Any], *, idempotency_key: str) -> tuple[int, Any]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)(ms|s|m)?", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"cannot read {value!r} as a duration, e.g. '5s'")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return {
        "ms": timedelta(milliseconds=amount),
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
    }[unit]


def wait_until(
    description: str, predicate: Any, *, timeout: float = MILESTONE_TIMEOUT_SECONDS
) -> Any:
    """Bounded wait. There is deliberately no unbounded sleep anywhere in this script."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(POLL_INTERVAL_SECONDS)
    raise DemoFailure(
        f"timed out after {timeout:.0f}s waiting for {description}; last saw {last!r}"
    )


def console_deliveries(children: list[Child]) -> list[dict[str, Any]]:
    """Every JSON line the console notification adapter printed, across all roles."""
    found: list[dict[str, Any]] = []
    for child in children:
        for line in child.lines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            with suppress(json.JSONDecodeError):
                document = json.loads(stripped)
                if isinstance(document, dict) and "delivery_key" in document:
                    found.append(document)
    return found


@contextmanager
def workspace_directory(keep: bool) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="agtyle-demo-"))
    try:
        yield directory
    finally:
        if keep:
            info(f"workspace preserved at {directory}")
        else:
            shutil.rmtree(directory, ignore_errors=True)


def run_demo(due_in: timedelta, *, keep_workspace: bool) -> int:
    with workspace_directory(keep_workspace) as workspace:
        demo = Demo(workspace=workspace, port=free_port())
        all_children: list[Child] = []
        try:
            # 1. Migrate and validate before any process role starts.
            demo.run_cli("init")
            demo.run_cli("validate")
            passed("database migrated and registry, schemas and Cedar policies validated")

            # 2-4. Start every role and wait for readiness.
            first_run = demo.start_all_roles()
            all_children.extend(first_run)

            def is_ready() -> Any:
                code, body = demo.get("/health/ready")
                return body if code == 200 and body and body.get("status") == "ok" else None

            readiness = wait_until("API readiness", is_ready, timeout=READINESS_TIMEOUT_SECONDS)
            passed("readiness")
            info(
                f"cedar {readiness['checks']['cedar_version']}, "
                f"agents {readiness['checks']['registry_agents']}"
            )

            # 5-6. Submit a real interaction with a wall-clock due time.
            due_at = datetime.now(UTC).replace(microsecond=0) + due_in
            due_text = due_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            status_code, response = demo.post(
                "/v1/interactions",
                {
                    "user_id": "user_local",
                    "conversation_id": "conv_demo",
                    "channel": "api",
                    "input": f"Remind me to verify Agtyle at {due_text}",
                    "preferred_execution_mode": "delegated",
                },
                idempotency_key="demo-reminder-001",
            )
            if status_code != 202:
                raise DemoFailure(f"interaction returned {status_code}: {response}")
            receipt = response["task_receipt"]
            task_id = receipt["task_id"]
            if receipt["assigned_agent_id"] != "steward" or receipt["status"] != "assigned":
                raise DemoFailure(f"unexpected receipt: {receipt}")
            passed("task accepted and assigned to steward")
            info(f"task {task_id}, reminder due at {due_text}")

            # 7. The Worker must reach a Cedar allow and store exactly one Reminder.
            def task_completed() -> Any:
                code, body = demo.get(f"/v1/tasks/{task_id}/timeline")
                if code != 200 or not body:
                    return None
                return body if body["current_state"]["status"] == "completed" else None

            timeline = wait_until("the task to complete", task_completed)
            decisions = [
                entry for entry in timeline["history"] if entry["kind"] == "policy_decision"
            ]
            if len(decisions) != 1 or "allow" not in decisions[0]["summary"]:
                raise DemoFailure(f"expected exactly one allow decision, saw {decisions}")
            passed("Cedar allowed reminder.create")

            reminders = [entry for entry in timeline["history"] if entry["kind"] == "reminder"]
            if len(reminders) != 1:
                raise DemoFailure(f"expected exactly one reminder, saw {len(reminders)}")
            reminder_id = reminders[0]["reference"]
            passed("reminder stored exactly once")

            # 8. The completion notification must actually be delivered to the console adapter.
            completion_key = f"task_terminal:{task_id}:completed"
            due_key = f"reminder_due:{reminder_id}:once"

            wait_until(
                "the task completion notification to be delivered",
                lambda: any(
                    item["delivery_key"] == completion_key
                    for item in console_deliveries(all_children)
                ),
            )
            passed("task completion notification delivered")

            # 9. Wall-clock scheduling: the reminder fires and is delivered.
            wait_until(
                "the reminder due notification to be delivered",
                lambda: any(
                    item["delivery_key"] == due_key for item in console_deliveries(all_children)
                ),
                timeout=MILESTONE_TIMEOUT_SECONDS + due_in.total_seconds(),
            )
            passed("reminder due notification delivered")

            deliveries_before = console_deliveries(all_children)
            code, reminder_body = demo.get(f"/v1/reminders/{reminder_id}")
            if code != 200 or reminder_body["status"] != "delivered":
                raise DemoFailure(f"reminder was not marked delivered: {reminder_body}")

            # 10-12. Stop everything, restart against the same database, and watch for duplicates.
            demo.stop_all()
            info("all roles stopped")

            second_run = demo.start_all_roles()
            all_children.extend(second_run)
            wait_until("readiness after restart", is_ready, timeout=READINESS_TIMEOUT_SECONDS)

            time.sleep(RESTART_OBSERVATION_SECONDS)
            deliveries_after = console_deliveries(all_children)
            counts: dict[str, int] = {}
            for item in deliveries_after:
                counts[item["delivery_key"]] = counts.get(item["delivery_key"], 0) + 1
            duplicates = {key: value for key, value in counts.items() if value > 1}
            if duplicates:
                raise DemoFailure(f"duplicate deliveries observed after restart: {duplicates}")
            if len(deliveries_after) != len(deliveries_before):
                raise DemoFailure(
                    f"restart produced {len(deliveries_after) - len(deliveries_before)} extra "
                    "deliveries"
                )
            passed("restart produced no duplicate")

            # 13. Print the timeline and the final record-count summary.
            code, final_timeline = demo.get(f"/v1/tasks/{task_id}/timeline")
            if code != 200:
                raise DemoFailure("timeline was unavailable after restart")
            record_counts = final_timeline["record_counts"]
            expected = {
                "agent_runs": 1,
                "action_requests": 1,
                "policy_decisions": 1,
                "action_results": 1,
                "reminders": 1,
                "notifications": 2,
            }
            mismatch = {
                key: (record_counts.get(key), value)
                for key, value in expected.items()
                if record_counts.get(key) != value
            }
            if mismatch:
                raise DemoFailure(f"record cardinality mismatch (actual, expected): {mismatch}")
            passed("timeline and record cardinality verified")

            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "reminder_id": reminder_id,
                        "cedar_version": readiness["checks"]["cedar_version"],
                        "record_counts": record_counts,
                        "delivery_keys": sorted(counts),
                        "current_state": final_timeline["current_state"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                flush=True,
            )
            return 0
        except DemoFailure as failure:
            failed(str(failure))
            for child in all_children:
                tail = child.lines()[-15:]
                if tail:
                    failed(f"--- {child.name} (last {len(tail)} lines) ---")
                    for line in tail:
                        failed(line)
            return 1
        finally:
            # Every path, including failure, terminates every child process.
            with suppress(DemoFailure):
                demo.stop_all()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--due-in",
        type=parse_duration,
        default=timedelta(seconds=5),
        help="how far in the future the reminder is due (default: 5s)",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="preserve the temporary data directory for diagnosis",
    )
    arguments = parser.parse_args(argv)
    return run_demo(arguments.due_in, keep_workspace=arguments.keep_workspace)


if __name__ == "__main__":
    raise SystemExit(main())
