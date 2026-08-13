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

TARGET_REPO = "xyh-nuage/knowledge"
EVIDENCE_COMMIT = "8395754382068c511e863c26d01229aed79fb193"
SILVER_REL = "evaluation_results/l1_chat_unseen_silver/2026-08-13_packet72d42247/silver_annotations.v1.json"
REVIEW1_REL = "evaluation_results/l1_chat_unseen_silver/2026-08-13_packet72d42247/silver_self_review.v1.json"
REVIEW2_REL = "evaluation_results/l1_chat_unseen_silver/2026-08-13_packet72d42247/silver_self_review.v2.json"
TEMPLATE_REL = "evaluation_results/l1_chat_unseen_blind_gold_packet/2026-08-12_a4982b68_run31586185049/gold_template.json"
ADJ_REL = "l1/evaluation/unseen/gold_adjudication_record.v1.json"
GEN_REL = "l1/evaluation/unseen/final_gold_freeze.py"
OUT_REL = "l1/evaluation/unseen/.runtime/final_gold/final_gold.v1.json"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def run(args, *, cwd, env, capture=False):
    return subprocess.run(args, cwd=cwd, env=env,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
        text=True, check=False)


def fetch_exact(dest: Path, sha: str, env: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for argv in (["git", "init", "-q"],
                 ["git", "remote", "add", "origin", f"https://github.com/{TARGET_REPO}.git"],
                 ["git", "fetch", "-q", "--depth=1", "origin", sha],
                 ["git", "checkout", "-q", "--detach", "FETCH_HEAD"]):
        if run(list(argv), cwd=dest, env=env).returncode != 0:
            raise RuntimeError("private fetch failed")
    cp = run(["git", "rev-parse", "HEAD"], cwd=dest, env=env, capture=True)
    if cp.returncode or cp.stdout.strip() != sha:
        raise RuntimeError("private SHA mismatch")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-sha", required=True)
    a = p.parse_args()
    source_sha = a.source_sha.lower()
    if SHA40.fullmatch(source_sha) is None:
        print("INVALID_SOURCE_SHA"); return 87
    token = os.environ.pop("KNOWLEDGE_READ_TOKEN", None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED"); return 86
    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    root = temp / "private-final-gold"
    source = root / "source"
    evidence = root / "evidence"
    askpass = temp / "private-final-gold-askpass.sh"
    shutil.rmtree(root, ignore_errors=True)
    askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token ;;\n*Password*) printf "%s\\n" "$KNOWLEDGE_READ_TOKEN" ;;\nesac\n', encoding="utf-8")
    askpass.chmod(0o700)
    env = os.environ.copy(); env["GIT_TERMINAL_PROMPT"] = "0"; env["GIT_ASKPASS"] = str(askpass); env["KNOWLEDGE_READ_TOKEN"] = token
    try:
        fetch_exact(source, source_sha, env)
        fetch_exact(evidence, EVIDENCE_COMMIT, env)
        out = source / OUT_REL
        cp = run([
            "python", str(source / GEN_REL),
            "--silver", str(evidence / SILVER_REL),
            "--review-v1", str(evidence / REVIEW1_REL),
            "--review-v2", str(evidence / REVIEW2_REL),
            "--adjudications", str(source / ADJ_REL),
            "--gold-template", str(evidence / TEMPLATE_REL),
            "--output", str(out),
        ], cwd=source, env=os.environ.copy(), capture=True)
        if cp.returncode != 0 or not out.is_file():
            print("FINAL_GOLD_GENERATION_FAILED"); return 23
        obj = json.loads(out.read_text(encoding="utf-8"))
        if obj.get("status") != "FROZEN" or int(obj.get("sample_size", -1)) != 80:
            raise RuntimeError("final Gold invariant failed")
        if int(obj.get("prediction_runs_completed_at_freeze", -1)) != 0:
            raise RuntimeError("prediction contamination")
        data = out.read_bytes()
        print(f"source_sha={source_sha}")
        print(f"evidence_commit={EVIDENCE_COMMIT}")
        print(f"sample_size={obj['sample_size']}")
        print(f"total_expected_units={obj['total_expected_units']}")
        print("prediction_runs_completed_at_freeze=0")
        print("final_gold_sha256=" + hashlib.sha256(data).hexdigest())
        print("FINAL_GOLD_REMOTE_PASS")
        return 0
    except Exception:
        print("FINAL_GOLD_REMOTE_FAILED"); return 24
    finally:
        env.pop("KNOWLEDGE_READ_TOKEN", None); askpass.unlink(missing_ok=True)

if __name__ == "__main__":
    raise SystemExit(main())
