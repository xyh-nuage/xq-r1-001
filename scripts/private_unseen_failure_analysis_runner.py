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
GOLD_COMMIT = "69df4097fa3742df876794260f296995e59b75fc"
FORMAL_RESULT_COMMIT = "265946c477dbd4156217a04c8a025aedfcfe3eaf"
GOLD_BASE = "evaluation_results/l1_chat_unseen_final_gold/2026-08-13_8778fc21_run31682050224"
SILVER_BASE = "evaluation_results/l1_chat_unseen_silver/2026-08-13_packet72d42247"
FORMAL_BASE = "evaluation_results/l1_chat_unseen_formal/2026-08-13_453cc48e_run31684555639"
RUNTIME_REL = "l1/evaluation/unseen/.runtime/failure_analysis"
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


def safe_print(summary: dict[str, object]) -> None:
    if summary.get("status") != "ANALYSIS_COMPLETE_PENDING_RESEARCH_REVIEW":
        raise RuntimeError("failure-analysis status mismatch")
    if summary.get("model_calls_performed") != 0:
        raise RuntimeError("failure analysis unexpectedly made model calls")
    if summary.get("formal_prediction_runs_completed") != 1:
        raise RuntimeError("formal prediction count drift")
    decomposition = summary.get("positive_case_decomposition")
    if not isinstance(decomposition, dict):
        raise RuntimeError("positive decomposition missing")
    parity = summary.get("input_parity")
    if not isinstance(parity, dict):
        raise RuntimeError("input parity summary missing")
    ranking = summary.get("primary_diagnostic_dimension_ranking_low_to_high_accuracy")
    if not isinstance(ranking, list):
        raise RuntimeError("dimension ranking missing")

    print("metric_model_calls_performed=0")
    print("metric_formal_prediction_runs_completed=1")
    for key in ("exact_structured_pass", "count_correct_structural_failure", "under_produced", "over_produced", "total_positive"):
        value = decomposition.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"unsafe decomposition metric: {key}")
        print(f"metric_positive_{key}={value}")
    for key in ("recorded_caveat_count", "parity_clean_by_recorded_provenance_count", "ambiguity_sensitive_case_count"):
        value = parity.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"unsafe parity metric: {key}")
        print(f"metric_parity_{key}={value}")
    caveat_ids = parity.get("recorded_caveat_ids")
    if not isinstance(caveat_ids, list) or any(not isinstance(x, str) or not re.fullmatch(r"U\d{3}", x) for x in caveat_ids):
        raise RuntimeError("unsafe parity IDs")
    print("metric_parity_recorded_caveat_ids=" + ",".join(caveat_ids))

    for row in ranking:
        if not isinstance(row, dict):
            raise RuntimeError("unsafe dimension row")
        dimension = str(row.get("dimension") or "")
        if not re.fullmatch(r"[a-z_.]+", dimension):
            raise RuntimeError("unsafe dimension name")
        declared = row.get("declared")
        incorrect = row.get("incorrect")
        accuracy = row.get("accuracy")
        if isinstance(declared, bool) or not isinstance(declared, int):
            raise RuntimeError("unsafe dimension denominator")
        if isinstance(incorrect, bool) or not isinstance(incorrect, int):
            raise RuntimeError("unsafe dimension error count")
        if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)):
            raise RuntimeError("unsafe dimension accuracy")
        print(f"metric_dimension_{dimension}_declared={declared}")
        print(f"metric_dimension_{dimension}_incorrect={incorrect}")
        print(f"metric_dimension_{dimension}_accuracy={float(accuracy):.12g}")
    print("metric_next_gate=PENDING_RESEARCH_REVIEW_AFTER_FAILURE_ANALYSIS")


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
    root = temp / "private-unseen-failure-analysis"
    source = root / "source"
    gold_root = root / "gold"
    formal_root = root / "formal"
    askpass = temp / "private-unseen-failure-analysis-askpass.sh"
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
        fetch_exact(gold_root, GOLD_COMMIT, env)
        fetch_exact(formal_root, FORMAL_RESULT_COMMIT, env)

        install = run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=source / "l1", env=os.environ.copy(), capture=True)
        if install.returncode != 0:
            print("FAILURE_ANALYSIS_SOURCE_INSTALL_FAILED")
            return 21

        tests = run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_unseen_failure_analysis.py"],
            cwd=source / "l1", env=os.environ.copy(), capture=True,
        )
        if tests.returncode != 0:
            print("FAILURE_ANALYSIS_SYNTHETIC_TESTS_FAILED")
            print(tests.stdout[-4000:])
            return 22
        print("metric_failure_analysis_synthetic_tests=4")

        runtime = source / RUNTIME_REL
        runtime.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m", "evaluation.unseen.failure_analysis",
            "--spec", "evaluation/unseen/failure_analysis_spec.v1.json",
            "--gold", str(gold_root / GOLD_BASE / "final_gold.v1.json"),
            "--predictions", str(formal_root / FORMAL_BASE / "predictions.json"),
            "--evaluation", str(formal_root / FORMAL_BASE / "evaluation.json"),
            "--construction", str(gold_root / GOLD_BASE / "gold_construction_contract.v2.json"),
            "--silver", str(gold_root / SILVER_BASE / "silver_annotations.v1.json"),
            "--review-v1", str(gold_root / SILVER_BASE / "silver_self_review.v1.json"),
            "--review-v2", str(gold_root / SILVER_BASE / "silver_self_review.v2.json"),
            "--adjudications", str(gold_root / GOLD_BASE / "gold_adjudication_record.v1.json"),
            "--blind-contract", "evaluation/unseen/blind_gold_contract.v1.json",
            "--output-dir", "evaluation/unseen/.runtime/failure_analysis",
        ]
        analysis = run(command, cwd=source / "l1", env=os.environ.copy(), capture=True)
        if analysis.returncode != 0:
            print("FAILURE_ANALYSIS_PROCESS_FAILED")
            print(analysis.stdout[-4000:])
            return 23

        expected_outputs = (
            runtime / "input_parity_audit.json",
            runtime / "dimension_failure_matrix.json",
            runtime / "failure_analysis_summary.json",
        )
        if any(not p.is_file() for p in expected_outputs):
            print("FAILURE_ANALYSIS_OUTPUT_MISSING")
            return 24
        summary = json.loads((runtime / "failure_analysis_summary.json").read_text(encoding="utf-8"))
        safe_print(summary)
        print(f"source_sha={source_sha}")
        print(f"gold_commit={GOLD_COMMIT}")
        print(f"formal_result_commit={FORMAL_RESULT_COMMIT}")
        print("L1_UNSEEN_FAILURE_ANALYSIS_REMOTE_PASS")
        return 0
    except Exception as exc:
        print("FAILURE_ANALYSIS_RUNNER_ERROR=" + type(exc).__name__)
        return 25
    finally:
        try:
            askpass.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
