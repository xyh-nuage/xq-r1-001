#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

TARGET_REPO = "xyh-nuage/knowledge"
PARENT_COMMIT = "9126027f111ffa9df2826d94ccc1b264423f71a7"
PARENT_BASE = "evaluation_results/l1_unseen_failure_analysis/2026-08-13_190f883a_run31688648607"
MATRIX_REL = f"{PARENT_BASE}/dimension_failure_matrix.json"
PARITY_REL = f"{PARENT_BASE}/input_parity_audit.json"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def run(args: list[str], *, cwd: Path | None, env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
        text=True,
        check=False,
    )


def fetch_exact(dest: Path, sha: str, env: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", f"https://github.com/{TARGET_REPO}.git"],
        ["git", "fetch", "-q", "--depth=1", "origin", sha],
        ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    ):
        cp = run(argv, cwd=dest, env=env)
        if cp.returncode != 0:
            raise RuntimeError("private git fetch failed")
    cp = run(["git", "rev-parse", "HEAD"], cwd=dest, env=env, capture=True)
    if cp.returncode != 0 or cp.stdout.strip() != sha:
        raise RuntimeError("private git SHA mismatch")


def emit_safe_stats(result: dict) -> None:
    partition = result["slice_partition"]
    for name in ("all_30", "exclude_u011", "parity_clean_non_ambiguity", "ambiguity_sensitive"):
        print(f"slice_{name}_cases={partition[name]}")
    for slice_name, slice_obj in result["slices"].items():
        for dim, stats in slice_obj["dimensions"].items():
            print(f"dimension_{slice_name}_{dim}_declared={stats['declared']}")
            accuracy = stats["accuracy"]
            print(f"dimension_{slice_name}_{dim}_accuracy={'NA' if accuracy is None else format(float(accuracy), '.12g')}")
        for relation, stats in slice_obj["error_cooccurrence"].items():
            prefix = f"co_{slice_name}_{relation}"
            for field in ("evaluable_pairs", "both_wrong", "left_only_wrong", "right_only_wrong", "neither_wrong"):
                print(f"{prefix}_{field}={stats[field]}")
            for field in ("p_right_wrong_given_left_wrong", "p_left_wrong_given_right_wrong"):
                value = stats[field]
                print(f"{prefix}_{field}={'NA' if value is None else format(float(value), '.12g')}")
    print("model_calls_performed=0")
    print("formal_prediction_runs_completed=1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    source_sha = args.source_sha.lower()
    if SHA40.fullmatch(source_sha) is None:
        print("INVALID_SOURCE_SHA")
        return 87

    token = os.environ.pop("KNOWLEDGE_READ_TOKEN", None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED")
        return 86

    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    root = temp / "private-unseen-failure-supplement"
    source = root / "source"
    parent = root / "parent-analysis"
    askpass = temp / "private-unseen-failure-supplement-askpass.sh"
    shutil.rmtree(root, ignore_errors=True)
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token ;;\n*Password*) printf "%s\\n" "$KNOWLEDGE_READ_TOKEN" ;;\nesac\n',
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass)
    env["KNOWLEDGE_READ_TOKEN"] = token

    try:
        fetch_exact(source, source_sha, env)
        fetch_exact(parent, PARENT_COMMIT, env)
        matrix = parent / MATRIX_REL
        parity = parent / PARITY_REL
        if not matrix.is_file() or not parity.is_file():
            raise RuntimeError("frozen parent analysis artifact missing")

        install = run(
            [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
            cwd=source / "l1",
            env=os.environ.copy(),
            capture=True,
        )
        if install.returncode != 0:
            raise RuntimeError("private source install failed")

        tests = run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_failure_analysis_supplement.py"],
            cwd=source / "l1",
            env=os.environ.copy(),
            capture=True,
        )
        if tests.returncode != 0:
            print(tests.stdout[-12000:])
            raise RuntimeError("supplement synthetic tests failed")
        print("supplement_synthetic_tests=4_passed")

        runtime = source / "l1/evaluation/unseen/.runtime/failure_analysis_supplement"
        runtime.mkdir(parents=True, exist_ok=True)
        output = runtime / "statistics.json"
        analysis = run(
            [
                sys.executable,
                "evaluation/unseen/failure_analysis_supplement.py",
                "--spec", "evaluation/unseen/failure_analysis_supplement_spec.v1.json",
                "--matrix", str(matrix),
                "--parity", str(parity),
                "--output", "evaluation/unseen/.runtime/failure_analysis_supplement/statistics.json",
            ],
            cwd=source / "l1",
            env=os.environ.copy(),
            capture=True,
        )
        if analysis.returncode != 0:
            print(analysis.stdout[-12000:])
            raise RuntimeError("supplement statistics failed")
        if not output.is_file():
            raise RuntimeError("supplement output missing")
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("model_calls_performed") != 0:
            raise RuntimeError("zero-model invariant failed")
        if result.get("formal_prediction_runs_completed") != 1:
            raise RuntimeError("formal prediction count drifted")
        emit_safe_stats(result)
        print(f"source_sha={source_sha}")
        print(f"parent_analysis_commit={PARENT_COMMIT}")
        print("FAILURE_ANALYSIS_SUPPLEMENT_REMOTE_PASS")
        return 0
    except Exception as exc:
        print(f"FAILURE_ANALYSIS_SUPPLEMENT_REMOTE_FAIL={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
