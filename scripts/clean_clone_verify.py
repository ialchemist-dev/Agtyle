#!/usr/bin/env python
"""Clone this repository at HEAD into a temporary directory and verify it there.

This is the acceptance test for the claim "someone else can reproduce this". The clone gets its
own data directory and its own virtual environment; it must not borrow this checkout's `.venv`,
its database, or any undeclared environment variable.

On success the temporary clone is removed. On failure its path is printed and preserved so the
failure can be diagnosed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Variables that would let the clone accidentally reuse this checkout's state.
STRIPPED_PREFIXES = ("AGTYLE_", "VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")

STEPS = (
    ("bootstrap", ["make", "bootstrap"]),
    ("init", ["make", "init"]),
    ("verify", ["make", "verify"]),
    ("demo-reminder", ["make", "demo-reminder"]),
)


def clean_environment(data_dir: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(key.startswith(prefix) for prefix in STRIPPED_PREFIXES)
    }
    env["AGTYLE_DATA_DIR"] = str(data_dir)
    env["AGTYLE_DATABASE_URL"] = f"sqlite:///{data_dir / 'agtyle.db'}"
    return env


def git(*arguments: str, cwd: Path = REPO_ROOT) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def run_step(name: str, command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    print(f"\n=== {name} ===", flush=True)
    started = time.monotonic()
    completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    return {
        "name": name,
        "command": " ".join(command),
        "exit_code": completed.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary clone even on success"
    )
    parser.add_argument("--skip-demo", action="store_true", help="skip the live demonstration step")
    arguments = parser.parse_args(argv)

    dirty = bool(git("status", "--porcelain"))
    commit = git("rev-parse", "HEAD")
    if dirty:
        print(
            "[WARN] the source worktree is dirty; the clone verifies the committed state at "
            f"{commit[:12]}, not the uncommitted changes",
            file=sys.stderr,
            flush=True,
        )

    workspace = Path(tempfile.mkdtemp(prefix="agtyle-clean-clone-"))
    clone = workspace / "Agyle"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    success = False
    try:
        print(f"[INFO] cloning {REPO_ROOT} at {commit[:12]} into {clone}", flush=True)
        cloned = subprocess.run(
            ["git", "clone", "--quiet", str(REPO_ROOT), str(clone)],
            capture_output=True,
            text=True,
            check=False,
        )
        if cloned.returncode != 0:
            print(f"[FAIL] clone failed: {cloned.stderr.strip()}", file=sys.stderr)
            return 1
        subprocess.run(
            ["git", "checkout", "--quiet", commit], cwd=clone, check=False, capture_output=True
        )

        env = clean_environment(data_dir)
        steps = [step for step in STEPS if not (arguments.skip_demo and step[0] == "demo-reminder")]
        for name, command in steps:
            result = run_step(name, command, cwd=clone, env=env)
            results.append(result)
            if result["exit_code"] != 0:
                print(f"\n[FAIL] {name} exited {result['exit_code']}", file=sys.stderr, flush=True)
                break
        else:
            success = True

        report = {
            "status": "passed" if success else "failed",
            "source_commit": commit,
            "source_dirty": dirty,
            "clone_path": str(clone),
            "python": sys.version.split()[0],
            "steps": results,
        }
        clone_report = clone / "artifacts" / "verification" / "verification-report.json"
        if clone_report.exists():
            report["clone_report"] = json.loads(clone_report.read_text(encoding="utf-8"))
            destination = REPO_ROOT / "artifacts" / "verification" / "clean-clone-report.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        print("\n" + json.dumps({k: v for k, v in report.items() if k != "clone_report"}, indent=2))
        return 0 if success else 1
    finally:
        if success and not arguments.keep:
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            print(f"[INFO] temporary clone preserved at {clone}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
