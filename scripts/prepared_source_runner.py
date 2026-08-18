#!/usr/bin/env python3
"""Run an audited plan in an already-prepared private source checkout.

The caller is responsible for preparing the exact immutable source checkout and
any immutable private inputs before this runner starts.  This runner receives no
GitHub source credential; it only validates HEAD, captures private logs, and
publishes safe pytest/numeric audit output.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

import private_source_runner as core


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--plan", required=True)
    ns = parser.parse_args()

    repo_dir = pathlib.Path(ns.source_dir).resolve()
    if not repo_dir.is_dir():
        print("PREPARED_SOURCE_REQUIRED")
        return 81
    if not (len(ns.source_sha) == 40 and all(c in "0123456789abcdef" for c in ns.source_sha.lower())):
        print("INVALID_SOURCE_SHA")
        return 82
    actual = subprocess.check_output(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != ns.source_sha:
        print("SOURCE_SHA_MISMATCH")
        return 83

    plan = json.loads(pathlib.Path(ns.plan).read_text(encoding="utf-8"))
    if plan.get("schema") != "remote_ci_plan.v1":
        print("INVALID_PLAN")
        return 84

    temp = pathlib.Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    logs = temp / "prepared-source-logs"
    logs.mkdir(parents=True, exist_ok=True)
    env_base = os.environ.copy()
    env_base.pop("KNOWLEDGE_READ_TOKEN", None)
    env_base["GIT_TERMINAL_PROMPT"] = "0"

    print(f"source_sha={actual}")
    for index, step in enumerate(plan.get("steps", []), start=1):
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            print("INVALID_PLAN_STEP")
            return 85
        try:
            cwd = core.safe_cwd(repo_dir, step.get("cwd"))
        except ValueError:
            print("INVALID_PLAN_CWD")
            return 86
        log_path = logs / f"step-{index}.log"
        with log_path.open("wb") as fh:
            proc = subprocess.run(argv, cwd=cwd, env=env_base, stdout=fh, stderr=subprocess.STDOUT)

        if core.is_pytest_step(argv):
            if not core.print_pytest_summary(index, log_path):
                print(f"step_{index}=FAIL")
                return 87
        if proc.returncode != 0:
            print(f"step_{index}=FAIL")
            return proc.returncode or 1
        audit = step.get("audit")
        if audit is not None and not core.print_numeric_json_audit(index, repo_dir, audit):
            print(f"step_{index}=FAIL")
            return 88
        print(f"step_{index}=PASS")

    print("REMOTE_PREPARED_PRIVATE_SOURCE_CI_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
