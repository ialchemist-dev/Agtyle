#!/usr/bin/env python
"""Enforce the coverage floors from the specification.

Coverage is a diagnostic, not proof of correctness, so these are floors rather than targets:

- 90% branch coverage for `domain` and `application`, where the business invariants live;
- 80% branch coverage overall.

Generated code, migrations and trivial CLI wiring are excluded in `pyproject.toml`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COVERAGE_JSON = REPO_ROOT / "artifacts" / "verification" / "coverage.json"

CORE_PREFIXES = ("src/agtyle/domain/", "src/agtyle/application/")
CORE_FLOOR = 90.0
OVERALL_FLOOR = 80.0


def branch_percent(
    covered_statements: int, statements: int, covered_branches: int, branches: int
) -> float:
    total = statements + branches
    if total == 0:
        return 100.0
    return 100.0 * (covered_statements + covered_branches) / total


def main() -> int:
    if not COVERAGE_JSON.exists():
        print(f"error: {COVERAGE_JSON} not found; run the coverage suite first", file=sys.stderr)
        return 1

    document = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    files = document["files"]

    core = {"statements": 0, "covered_statements": 0, "branches": 0, "covered_branches": 0}
    for path, entry in files.items():
        normalized = path.replace("\\", "/")
        if not normalized.startswith(CORE_PREFIXES):
            continue
        summary = entry["summary"]
        core["statements"] += summary["num_statements"]
        core["covered_statements"] += summary["covered_lines"]
        core["branches"] += summary["num_branches"]
        core["covered_branches"] += summary["covered_branches"]

    core_percent = branch_percent(
        core["covered_statements"], core["statements"], core["covered_branches"], core["branches"]
    )
    overall_percent = float(document["totals"]["percent_covered"])

    failures: list[str] = []
    if core_percent < CORE_FLOOR:
        failures.append(
            f"domain and application branch coverage is {core_percent:.1f}%, floor is {CORE_FLOOR}%"
        )
    if overall_percent < OVERALL_FLOOR:
        failures.append(
            f"overall branch coverage is {overall_percent:.1f}%, floor is {OVERALL_FLOOR}%"
        )

    print(
        json.dumps(
            {
                "core_branch_coverage": round(core_percent, 2),
                "overall_branch_coverage": round(overall_percent, 2),
                "status": "failed" if failures else "passed",
            },
            indent=2,
            sort_keys=True,
        )
    )
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
