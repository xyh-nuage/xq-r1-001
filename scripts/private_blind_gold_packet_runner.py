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
CONTRACT_REL = "l1/evaluation/unseen/blind_gold_contract.v1.json"
OUTPUT_REL = "l1/evaluation/unseen/.runtime/blind_gold_packet"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
PASSED = re.compile(r"(\d+) passed")


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


def fetch_exact(dest: Path, sha: str, *, env: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", f"https://github.com/{TARGET_REPO}.git"],
        ["git", "fetch", "-q", "--depth=1", "origin", sha],
        ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    ]
    for argv in steps:
        cp = run(argv, cwd=dest, env=env)
        if cp.returncode != 0:
            raise RuntimeError("private git fetch failed")
    actual = run(["git", "rev-parse", "HEAD"], cwd=dest, env=env, capture=True)
    if actual.returncode != 0 or actual.stdout.strip() != sha:
        raise RuntimeError("private git SHA mismatch")


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
    root = temp / "private-blind-gold"
    source_root = root / "source"
    manifest_root = root / "manifest"
    askpass = temp / "private-blind-gold-askpass.sh"
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
        fetch_exact(source_root, source_sha, env=env)
        contract_path = source_root / CONTRACT_REL
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        frozen = contract["frozen_manifest"]
        manifest_commit = str(frozen["commit"]).lower()
        manifest_rel = str(frozen["path"])
        if SHA40.fullmatch(manifest_commit) is None:
            raise RuntimeError("invalid manifest commit")
        fetch_exact(manifest_root, manifest_commit, env=env)
        manifest_path = manifest_root / manifest_rel
        if not manifest_path.is_file():
            raise RuntimeError("frozen manifest missing")

        test_cp = run(
            [sys.executable, "-m", "pytest", "-q", "l1/tests/test_blind_gold_packet.py"],
            cwd=source_root,
            env=os.environ.copy(),
            capture=True,
        )
        if test_cp.returncode != 0:
            print("blind_gold_packet_tests=FAIL")
            return 21
        match = PASSED.search(test_cp.stdout or "")
        if match is None:
            print("blind_gold_packet_tests=SUMMARY_UNAVAILABLE")
            return 22
        tests_passed = int(match.group(1))

        output_dir = source_root / OUTPUT_REL
        gen_cp = run(
            [
                sys.executable,
                str(source_root / "l1/evaluation/unseen/blind_gold_packet.py"),
                "--repo-root", str(source_root),
                "--contract", str(contract_path),
                "--manifest", str(manifest_path),
                "--output-dir", str(output_dir),
            ],
            cwd=source_root,
            env=os.environ.copy(),
            capture=True,
        )
        if gen_cp.returncode != 0:
            print("blind_gold_packet_generation=FAIL")
            return 23

        report = json.loads((output_dir / "packet_report.json").read_text(encoding="utf-8"))
        safe_ints = [
            "sample_size",
            "packet_line_count",
            "binding_line_count",
            "gold_template_case_count",
            "public_message_bodies_emitted",
            "human_gold_records_completed",
            "model_labels_generated",
            "prediction_runs_completed",
        ]
        safe_hashes = [
            "selected_identity_set_sha256",
            "contract_sha256",
            "frozen_manifest_sha256",
            "annotation_packet_sha256",
            "bindings_sha256",
            "gold_template_sha256",
        ]
        print(f"source_sha={source_sha}")
        print(f"manifest_commit={manifest_commit}")
        print(f"targeted_tests_passed={tests_passed}")
        for name in safe_ints:
            value = report.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RuntimeError("unsafe integer audit metric")
            print(f"metric_{name}={value}")
        for name in safe_hashes:
            value = str(report.get(name) or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise RuntimeError("unsafe hash audit metric")
            print(f"metric_{name}={value}")
        print("BLIND_GOLD_PACKET_REMOTE_PASS")
        return 0
    except Exception:
        print("BLIND_GOLD_PACKET_REMOTE_FAILED")
        return 24
    finally:
        env.pop("KNOWLEDGE_READ_TOKEN", None)
        token = None
        askpass.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
