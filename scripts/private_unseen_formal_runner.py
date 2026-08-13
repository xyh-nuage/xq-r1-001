#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

TARGET_REPO = "xyh-nuage/knowledge"
MANIFEST_COMMIT = "911a05dfee7488e3ebddea69d5b815d4f0dddb5b"
MANIFEST_REL = "evaluation_results/l1_chat_unseen_manifest_freeze/2026-08-12_86bd82c7_run31580420134/unseen_manifest.v1.json"
GOLD_COMMIT = "69df4097fa3742df876794260f296995e59b75fc"
GOLD_REL = "evaluation_results/l1_chat_unseen_final_gold/2026-08-13_8778fc21_run31682050224/final_gold.v1.json"
RUNTIME_REL = "l1/evaluation/unseen/.runtime/formal"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def require_exact_model_config() -> None:
    required = ("L1_LLM_API_KEY", "L1_LLM_BASE_URL", "L1_LLM_MODEL")
    if any(not os.environ.get(name) for name in required):
        raise RuntimeError("model credential/config missing")
    if "deepseek" not in os.environ["L1_LLM_MODEL"].lower():
        raise RuntimeError("configured model is not DeepSeek")
    expected = {
        "L1_LLM_TIMEOUT_SECONDS": "60",
        "L1_LLM_MAX_RETRIES": "2",
        "L1_LLM_RETRY_BACKOFF_SECONDS": "1",
        "L1_LLM_MAX_COMPLETION_TOKENS": "3000",
        "L1_LLM_MAX_COMPLETION_FIELD": "max_tokens",
        "L1_LLM_DISABLE_REASONING": "true",
    }
    for name, value in expected.items():
        if os.environ.get(name) != value:
            raise RuntimeError(f"formal model config mismatch: {name}")


def safe_report(report: dict, pred_summary: dict) -> None:
    int_fields = [
        "sample_size", "gold_units", "predicted_units", "matched_units", "exact_case_count",
        "unit_count_exact_case_count", "zero_output_gold_cases", "zero_output_correct_cases",
        "nonzero_gold_cases", "nonzero_exact_cases", "runtime_failure_cases", "validation_errors",
        "model_calls", "discovery_calls", "assessment_calls", "artifact_knowledge_calls",
        "prompt_tokens", "completion_tokens", "total_tokens", "retries", "formal_prediction_runs_completed",
    ]
    float_fields = [
        "unit_precision", "unit_recall", "unit_f1", "exact_case_accuracy",
        "unit_count_exact_accuracy", "zero_output_accuracy", "nonzero_exact_case_accuracy",
    ]
    for name in int_fields:
        value = report.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"unsafe/missing integer metric: {name}")
        print(f"metric_{name}={value}")
    for name in float_fields:
        value = report.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"unsafe/missing float metric: {name}")
        print(f"metric_{name}={float(value):.12g}")
    for name in ("gold_sha256", "predictions_sha256", "evaluation_sha256"):
        value = str(report.get(name) or "").lower()
        if SHA256.fullmatch(value) is None:
            raise RuntimeError(f"unsafe/missing hash metric: {name}")
        print(f"metric_{name}={value}")
    model_ids = report.get("model_ids") or []
    if not isinstance(model_ids, list) or not model_ids or any(not isinstance(v, str) or "deepseek" not in v.lower() for v in model_ids):
        raise RuntimeError("formal result model IDs are not DeepSeek")
    print("metric_model_ids=" + ",".join(model_ids))
    print("metric_thinking_mode=" + str(report.get("thinking_mode") or ""))
    print("metric_selected_identity_set_sha256=" + str(report.get("selected_identity_set_sha256") or ""))
    print("metric_formal_prediction_passes=1")
    print("metric_gold_loaded_during_prediction=0")
    print("metric_evaluator_loaded_during_prediction=0")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-sha", required=True)
    args = p.parse_args()
    source_sha = args.source_sha.lower()
    if SHA40.fullmatch(source_sha) is None:
        print("INVALID_SOURCE_SHA")
        return 87

    token = os.environ.pop("KNOWLEDGE_READ_TOKEN", None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED")
        return 86
    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    root = temp / "private-unseen-formal"
    source = root / "source"
    manifest_root = root / "manifest"
    gold_root = root / "gold"
    askpass = temp / "private-unseen-formal-askpass.sh"
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

    model_started = False
    try:
        # Everything below this point and before the prediction subprocess is a zero-model-call preflight.
        require_exact_model_config()
        fetch_exact(source, source_sha, env)
        fetch_exact(manifest_root, MANIFEST_COMMIT, env)
        manifest_path = manifest_root / MANIFEST_REL
        if not manifest_path.is_file():
            raise RuntimeError("frozen manifest missing")

        install = run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=source / "l1", env=os.environ.copy(), capture=True)
        if install.returncode != 0:
            raise RuntimeError("private source install failed")

        runtime = source / RUNTIME_REL
        runtime.mkdir(parents=True, exist_ok=True)
        model_started = True
        prediction = run(
            [
                sys.executable,
                "tools/run_unseen_formal_predictions.py",
                "--repo-root", "..",
                "--contract", "evaluation/unseen/formal_unseen_execution_contract.v1.json",
                "--manifest", str(manifest_path),
                "--profiles", "config/candidate_scene_profiles.example.v1.json",
                "--output-dir", "evaluation/unseen/.runtime/formal",
            ],
            cwd=source / "l1",
            env=os.environ.copy(),
            capture=True,
        )
        if prediction.returncode != 0:
            print("FORMAL_PREDICTION_PROCESS_FAILED_AFTER_MODEL_START")
            return 31
        predictions_path = runtime / "predictions.json"
        pred_summary_path = runtime / "prediction_summary.json"
        if not predictions_path.is_file() or not pred_summary_path.is_file():
            print("FORMAL_PREDICTION_OUTPUT_MISSING_AFTER_MODEL_START")
            return 32
        pred_summary = json.loads(pred_summary_path.read_text(encoding="utf-8"))
        if int(pred_summary.get("formal_prediction_runs_completed", -1)) != 1 or int(pred_summary.get("sample_size", -1)) != 80:
            print("FORMAL_PREDICTION_INVARIANT_FAILED_AFTER_MODEL_START")
            return 33

        # Gold is deliberately fetched only after all 80 prediction cases are complete.
        fetch_exact(gold_root, GOLD_COMMIT, env)
        gold_path = gold_root / GOLD_REL
        if not gold_path.is_file():
            print("FORMAL_GOLD_FETCH_FAILED_AFTER_MODEL_START")
            return 34
        score = run(
            [
                sys.executable,
                "tools/score_unseen_formal_predictions.py",
                "--contract", "evaluation/unseen/formal_unseen_execution_contract.v1.json",
                "--evaluator-contract", "evaluation/unseen/unseen_evaluator_contract.v1.json",
                "--gold", str(gold_path),
                "--predictions", "evaluation/unseen/.runtime/formal/predictions.json",
                "--prediction-summary", "evaluation/unseen/.runtime/formal/prediction_summary.json",
                "--output-dir", "evaluation/unseen/.runtime/formal",
            ],
            cwd=source / "l1",
            env=os.environ.copy(),
            capture=True,
        )
        if score.returncode != 0:
            print("FORMAL_SCORING_FAILED_AFTER_MODEL_START")
            return 35
        report_path = runtime / "formal_report.json"
        evaluation_path = runtime / "evaluation.json"
        if not report_path.is_file() or not evaluation_path.is_file():
            print("FORMAL_SCORING_OUTPUT_MISSING_AFTER_MODEL_START")
            return 36
        report = json.loads(report_path.read_text(encoding="utf-8"))
        safe_report(report, pred_summary)
        print(f"source_sha={source_sha}")
        print(f"manifest_commit={MANIFEST_COMMIT}")
        print(f"gold_commit={GOLD_COMMIT}")
        print("FORMAL_UNSEEN_REMOTE_PASS")
        return 0
    except Exception as exc:
        if model_started:
            print(f"FORMAL_UNSEEN_FAILED_AFTER_MODEL_START:{type(exc).__name__}")
            return 39
        print(f"FORMAL_UNSEEN_PREFLIGHT_FAILED_ZERO_MODEL_CALLS:{type(exc).__name__}")
        return 24
    finally:
        env.pop("KNOWLEDGE_READ_TOKEN", None)
        token = None
        askpass.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
