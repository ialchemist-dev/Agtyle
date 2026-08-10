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
        "performance": None,
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
