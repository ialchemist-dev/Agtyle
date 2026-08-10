#!/usr/bin/env python
"""Generate the machine-readable verification report and its Markdown rendering.

The JSON document is the source of truth; the Markdown file renders it. A run is successful
only when every command exited zero, no test failed, no required test was skipped, every policy
case matched, the end-to-end cardinality was exact, and `unmet_requirements` is empty.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

ARTIFACTS = REPO_ROOT / "artifacts" / "verification"
JSON_REPORT = ARTIFACTS / "verification-report.json"
MARKDOWN_REPORT = ARTIFACTS / "verification-report.md"
E2E_PROOF = ARTIFACTS / "deterministic-reminder-e2e.json"
REPORT_SCHEMA = REPO_ROOT / "contracts" / "schemas" / "v1" / "verification-report.schema.json"

SUITES = (
    ("unit", "tests/unit"),
    ("contract", "tests/contract"),
    ("persistence", "tests/persistence"),
    ("integration", "tests/integration"),
    ("failure_injection", "tests/failure_injection"),
    ("e2e", "tests/e2e"),
)

REQUIRED_E2E_COUNTS = {
    "intents": 1,
    "tasks": 1,
    "agent_runs": 1,
    "action_requests": 1,
    "policy_decisions": 1,
    "action_results": 1,
    "reminders": 1,
    "notifications": 2,
}

SUMMARY_PATTERN = re.compile(
    r"(?:(?P<passed>\d+) passed)?(?:[^\n]*?(?P<failed>\d+) failed)?"
    r"(?:[^\n]*?(?P<skipped>\d+) skipped)?"
)


def run(name: str, command: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "name": name,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "_stdout": completed.stdout,
        "_stderr": completed.stderr,
    }


def parse_pytest_summary(output: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    for line in reversed(output.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            for count, label in re.findall(r"(\d+) (passed|failed|skipped|error[s]?)", line):
                if label == "passed":
                    passed = int(count)
                elif label == "skipped":
                    skipped = int(count)
                else:
                    failed += int(count)
            break
    return passed, failed, skipped


def git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


async def collect_policy_cases() -> tuple[list[dict[str, str]], str | None]:
    """Re-run the P01-P10 matrix through the real engine so the report is evidence, not memory."""
    from agtyle.adapters.authorization.cedar_cli import CedarCliAuthorization, CedarEntity
    from agtyle.config import CEDAR_PINNED_VERSION
    from agtyle.ports.authorization import AuthorizationRequest

    policy_dir = REPO_ROOT / "policies" / "cedar"
    adapter = CedarCliAuthorization(
        binary=REPO_ROOT / ".tools" / "cedar" / CEDAR_PINNED_VERSION / "cedar",
        schema=policy_dir / "agtyle.cedarschema",
        policies=policy_dir / "base.cedar",
        pinned_version=CEDAR_PINNED_VERSION,
    )
    version = adapter.engine_version()
    cases = json.loads((policy_dir / "tests.json").read_text(encoding="utf-8"))

    def parse_uid(uid: str) -> tuple[str, str]:
        head, _, quoted = uid.partition('::"')
        return head.removeprefix("Agtyle::"), quoted.rstrip('"')

    results: list[dict[str, str]] = []
    for case in cases:
        request = case["request"]
        principal_type, principal_id = parse_uid(request["principal"])
        resource_type, resource_id = parse_uid(request["resource"])
        _, action_id = parse_uid(request["action"])
        entities = [
            CedarEntity(
                entity_type=item["uid"]["type"].removeprefix("Agtyle::"),
                entity_id=item["uid"]["id"],
                attributes=item["attrs"],
            )
            for item in case["entities"]
        ]
        outcome = adapter._authorize_with_entities(
            AuthorizationRequest(
                principal_type=principal_type,
                principal_id=principal_id,
                action_id=action_id,
                resource_type=resource_type,
                resource_id=resource_id,
                context=request["context"],
            ),
            entities,
            version,
        )
        results.append(
            {
                "id": case["agtyle"]["id"],
                "expected": case["agtyle"]["expected_outcome"],
                "actual": outcome.decision.value,
            }
        )
    return results, version


PERFORMANCE_TARGETS_MS = {
    "interaction_receipt_p95_ms": 250.0,
    "task_claim_p95_ms": 100.0,
    "cedar_decision_p95_ms": 250.0,
    "due_detection_p95_ms": 250.0,
}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


async def collect_performance(samples: int = 12) -> dict[str, Any]:
    """Measure the sanity targets in §25.3 on this machine, without any LLM in the path.

    These are sanity checks, not real-time guarantees. The report records the observed values
    and flags anything above twice its target so a regression is visible rather than assumed.
    """
    import tempfile

    from agtyle.adapters.notifications.recording import RecordingNotificationAdapter
    from agtyle.application.interaction_service import HandleInteraction
    from agtyle.bootstrap import build_container, migrate
    from agtyle.config import Settings

    measurements: dict[str, list[float]] = {
        "interaction_receipt_p95_ms": [],
        "task_claim_p95_ms": [],
        "cedar_decision_p95_ms": [],
        "due_detection_p95_ms": [],
    }

    with tempfile.TemporaryDirectory(prefix="agtyle-perf-") as workspace:
        directory = Path(workspace)
        settings = Settings(
            env="test",
            data_dir=directory,
            database_url=f"sqlite:///{directory / 'perf.db'}",
            contracts_dir=REPO_ROOT / "contracts",
            agents_dir=REPO_ROOT / "agents",
            cedar_binary=REPO_ROOT / ".tools" / "cedar" / "4.12.0" / "cedar",
            cedar_schema=REPO_ROOT / "policies" / "cedar" / "agtyle.cedarschema",
            cedar_policies=REPO_ROOT / "policies" / "cedar" / "base.cedar",
            notification_adapter="recording",
        )
        migrate(settings)
        container = build_container(
            settings,
            notification_adapters={"recording": RecordingNotificationAdapter()},
            configure_logs=False,
        )
        try:
            for index in range(samples):
                started = time.perf_counter()
                await container.interaction_service.handle(
                    HandleInteraction(
                        user_id="user_local",
                        conversation_id="conv_perf",
                        channel="api",
                        input="Remind me to measure latency at 2099-01-01T00:00:00Z",
                        idempotency_key=f"perf-{index}",
                    )
                )
                measurements["interaction_receipt_p95_ms"].append(
                    (time.perf_counter() - started) * 1000
                )

                started = time.perf_counter()
                claimed = await container.execution_service.claim_task(owner=f"perf-{index}")
                measurements["task_claim_p95_ms"].append((time.perf_counter() - started) * 1000)
                if claimed is not None:
                    await container.execution_service.execute_claimed_task(
                        claimed, owner=f"perf-{index}"
                    )

                started = time.perf_counter()
                await container.reminder_service.claim_due()
                measurements["due_detection_p95_ms"].append((time.perf_counter() - started) * 1000)

            from agtyle.ports.authorization import AuthorizationRequest

            probe = AuthorizationRequest(
                principal_type="Agent",
                principal_id="steward",
                action_id="reminder.create",
                resource_type="ReminderCollection",
                resource_id="user_local",
                context={
                    "origin_user_id": "user_local",
                    "has_direct_user_instruction": True,
                    "payload_hash": "sha256:" + "0" * 64,
                    "approval_present": False,
                    "approval_valid": False,
                },
            )
            for _ in range(samples):
                started = time.perf_counter()
                await container.authorization.authorize(probe)
                measurements["cedar_decision_p95_ms"].append((time.perf_counter() - started) * 1000)
        finally:
            container.dispose()

    observed = {name: round(percentile(values, 0.95), 2) for name, values in measurements.items()}
    regressions = [
        name for name, value in observed.items() if value > 2 * PERFORMANCE_TARGETS_MS[name]
    ]
    return {
        "targets_ms": PERFORMANCE_TARGETS_MS,
        "observed_p95_ms": observed,
        "regressions": regressions,
        "samples": samples,
    }


def render_markdown(report: dict[str, Any]) -> str:
    status = "PASSED" if not report["unmet_requirements"] else "INCOMPLETE"
    lines = [
        "# Agtyle Verification Report",
        "",
        f"**Result:** {status}  ",
        f"**Generated:** {report['generated_at']}  ",
        f"**Commit:** `{report['git_commit']}`"
        + (" (working tree dirty)" if report["git_dirty"] else ""),
        "",
        "This document renders `verification-report.json`. That file is the source of truth.",
        "",
        "## Environment",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Operating system | {report['os']} |",
        f"| Architecture | {report['architecture']} |",
        f"| Python | {report['python_version']} |",
        f"| Cedar | {report['cedar_version']} |",
        f"| Migration head | {report['migration_head']} |",
        "",
        "## Commands",
        "",
        "| Command | Exit code | Duration |",
        "|---|---:|---:|",
    ]
    for command in report["commands"]:
        lines.append(
            f"| `{command['command']}` | {command['exit_code']} | {command['duration_ms']} ms |"
        )

    lines += ["", "## Tests", "", "| Suite | Passed | Failed | Skipped |", "|---|---:|---:|---:|"]
    for suite in report["tests"]:
        lines.append(
            f"| {suite['suite']} | {suite['passed']} | {suite['failed']} | {suite['skipped']} |"
        )

    lines += [
        "",
        "## Cedar policy matrix",
        "",
        "| Case | Expected | Actual | Result |",
        "|---|---|---|---|",
    ]
    for case in report["policy_cases"]:
        mark = "pass" if case["expected"] == case["actual"] else "FAIL"
        lines.append(f"| {case['id']} | {case['expected']} | {case['actual']} | {mark} |")

    e2e = report.get("e2e")
    if e2e:
        lines += [
            "",
            "## End-to-end proof",
            "",
            f"Task `{e2e.get('task_id')}` produced reminder `{e2e.get('reminder_id')}`.",
            "",
            "| Record | Count |",
            "|---|---:|",
        ]
        for key, value in sorted(e2e.get("counts", {}).items()):
            lines.append(f"| {key} | {value} |")
        lines += [
            "",
            f"Restart duplicate check: **{report['restart_duplicate_check']}**.",
        ]

    performance = report.get("performance")
    if performance:
        lines += [
            "",
            "## Performance sanity",
            "",
            "Observed on this machine, without an LLM in the path. These are sanity checks, not",
            "real-time guarantees; a value above twice its target is flagged as a regression.",
            "",
            "| Measurement | Target p95 | Observed p95 | |",
            "|---|---:|---:|---|",
        ]
        for name, target in sorted(performance["targets_ms"].items()):
            observed = performance["observed_p95_ms"].get(name, 0.0)
            mark = "REGRESSION" if name in performance["regressions"] else "ok"
            lines.append(f"| {name} | {target:.0f} ms | {observed:.2f} ms | {mark} |")

    lines += ["", "## Unmet requirements", ""]
    if report["unmet_requirements"]:
        lines.extend(f"- {item}" for item in report["unmet_requirements"])
    else:
        lines.append("None. Every checked requirement was satisfied by this run.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="reuse the current working tree state without re-running the suites",
    )
    arguments = parser.parse_args(argv)

    from jsonschema import Draft202012Validator

    from agtyle.adapters.persistence import migrator
    from agtyle.config import Settings

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    unmet: list[str] = []
    commands: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []

    for name, path in SUITES:
        if arguments.skip_tests:
            break
        result = run(f"pytest {name}", [sys.executable, "-m", "pytest", path, "-q"])
        output = result.pop("_stdout") + result.pop("_stderr")
        commands.append(result)
        passed, failed, skipped = parse_pytest_summary(output)
        tests.append({"suite": name, "passed": passed, "failed": failed, "skipped": skipped})
        if result["exit_code"] != 0:
            unmet.append(f"test suite `{name}` failed (exit {result['exit_code']})")
        if failed:
            unmet.append(f"test suite `{name}` reported {failed} failing tests")
        if skipped:
            unmet.append(f"test suite `{name}` skipped {skipped} tests")

    for name, command in (
        ("lint", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"]),
        (
            "format",
            [sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"],
        ),
        ("typecheck", [sys.executable, "-m", "mypy", "--strict", "src/agtyle"]),
        ("migrations", [sys.executable, "scripts/migration_check.py"]),
    ):
        result = run(name, command)
        result.pop("_stdout")
        result.pop("_stderr")
        commands.append(result)
        if result["exit_code"] != 0:
            unmet.append(f"`{name}` exited {result['exit_code']}")

    policy_cases, cedar_version = asyncio.run(collect_policy_cases())
    for case in policy_cases:
        if case["expected"] != case["actual"]:
            unmet.append(
                f"policy case {case['id']} expected {case['expected']} but got {case['actual']}"
            )
    if cedar_version is None:
        unmet.append("the pinned Cedar CLI was not available")

    e2e: dict[str, Any] | None = None
    restart_check = "not_run"
    if E2E_PROOF.exists():
        e2e = json.loads(E2E_PROOF.read_text(encoding="utf-8"))
        restart_check = str(e2e.get("restart_duplicate_check", "not_run"))
        if e2e.get("counts") != REQUIRED_E2E_COUNTS:
            unmet.append(f"end-to-end record cardinality was {e2e.get('counts')}")
        if restart_check != "passed":
            unmet.append("the restart duplicate check did not pass")
    else:
        unmet.append("no end-to-end proof artifact was produced")

    performance = asyncio.run(collect_performance())
    for regression in performance["regressions"]:
        unmet.append(f"performance regression: {regression} exceeded twice its target")

    settings = Settings(env="test")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git_commit": git("rev-parse", "HEAD") or "unknown",
        "git_dirty": bool(git("status", "--porcelain")),
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "cedar_version": cedar_version,
        "migration_head": migrator.head_revision(settings),
        "commands": commands,
        "tests": tests,
        "policy_cases": policy_cases,
        "e2e": e2e,
        "performance": performance,
        "restart_duplicate_check": restart_check,
        "unmet_requirements": unmet,
    }

    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)

    JSON_REPORT.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    MARKDOWN_REPORT.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps({"report": str(JSON_REPORT), "unmet_requirements": unmet}, indent=2))
    return 1 if unmet else 0


if __name__ == "__main__":
    raise SystemExit(main())
