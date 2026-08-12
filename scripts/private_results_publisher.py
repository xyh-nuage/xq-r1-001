#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
EVAL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DATE_RE = re.compile(r"^20[0-9]{2}-[0-9]{2}-[0-9]{2}$")
ALLOWED_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".env", ".p12", ".pfx"}
TARGET_REPO = "xyh-nuage/knowledge"
TARGET_BRANCH = "evaluation-results"
TARGET_ROOT = "evaluation_results"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_rel(value: str) -> PurePosixPath:
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("unsafe relative path")
    return rel


def inside(root: Path, rel_value: str) -> Path:
    rel = safe_rel(rel_value)
    resolved = (root / Path(*rel.parts)).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("path escapes root")
    return resolved


def git(args: list[str], *, cwd: Path, env: dict[str, str], capture: bool = False) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError("git command failed")
    return (cp.stdout or "").strip()


def parse_copy(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("copy must be SRC=DEST")
    src, dest = value.split("=", 1)
    safe_rel(src)
    safe_rel(dest)
    return src, dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--harness-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--date", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--copy", action="append", default=[])
    ns = parser.parse_args()

    source_sha = ns.source_sha.lower()
    harness_sha = ns.harness_sha.lower()
    if SHA40_RE.fullmatch(source_sha) is None or SHA40_RE.fullmatch(harness_sha) is None:
        print("PRIVATE_RESULT_BINDING_INVALID")
        return 87
    if not str(ns.run_id).isdigit() or (ns.job_id and not str(ns.job_id).isdigit()):
        print("PRIVATE_RESULT_RUN_ID_INVALID")
        return 87
    if DATE_RE.fullmatch(ns.date) is None or EVAL_RE.fullmatch(ns.evaluation) is None:
        print("PRIVATE_RESULT_NAME_INVALID")
        return 87
    if not ns.copy:
        print("PRIVATE_RESULT_FILESET_EMPTY")
        return 88

    token = os.environ.pop("KNOWLEDGE_RESULTS_WRITE_TOKEN", None)
    if not token:
        print("PRIVATE_RESULTS_WRITE_CREDENTIAL_REQUIRED")
        return 86

    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    source_root = Path(ns.source_root).resolve()
    if source_root != temp and temp not in source_root.parents:
        print("PRIVATE_RESULT_SOURCE_ROOT_INVALID")
        return 89

    result_rel = PurePosixPath(
        TARGET_ROOT,
        ns.evaluation,
        f"{ns.date}_{source_sha[:8]}_run{ns.run_id}",
    )
    publish_root = temp / "private-results-publish"
    askpass = temp / "private-results-askpass.sh"
    shutil.rmtree(publish_root, ignore_errors=True)
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token ;;\n*Password*) printf "%s\\n" "$KNOWLEDGE_RESULTS_WRITE_TOKEN" ;;\nesac\n',
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass)
    env["KNOWLEDGE_RESULTS_WRITE_TOKEN"] = token

    try:
        publish_root.mkdir(parents=True, exist_ok=True)
        git(["init", "-q"], cwd=publish_root, env=env)
        git(["remote", "add", "origin", f"https://github.com/{TARGET_REPO}.git"], cwd=publish_root, env=env)
        git(["fetch", "-q", "--depth=1", "origin", TARGET_BRANCH], cwd=publish_root, env=env)
        git(["checkout", "-q", "-B", TARGET_BRANCH, "FETCH_HEAD"], cwd=publish_root, env=env)

        target_dir = inside(publish_root, result_rel.as_posix())
        if target_dir.exists():
            print("PRIVATE_RESULT_ALREADY_EXISTS")
            return 90
        target_dir.mkdir(parents=True, exist_ok=False)

        copied: list[dict[str, object]] = []
        total_bytes = 0
        for raw in ns.copy:
            src_rel, dest_rel = parse_copy(raw)
            src = inside(source_root, src_rel)
            dest = inside(target_dir, dest_rel)
            if not src.is_file() or src.is_symlink():
                raise ValueError("source result file invalid")
            suffix = src.suffix.lower()
            if suffix in FORBIDDEN_SUFFIXES or suffix not in ALLOWED_SUFFIXES:
                raise ValueError("result file type not allowed")
            size = src.stat().st_size
            if size <= 0 or size > MAX_FILE_BYTES:
                raise ValueError("result file size invalid")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("result set too large for Git persistence")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            copied.append({
                "source_path": src_rel,
                "stored_path": dest_rel,
                "bytes": size,
                "sha256": sha256_file(dest),
            })

        run_record = {
            "schema_version": "private_evaluation_result.v1",
            "source_repository": TARGET_REPO,
            "source_sha": source_sha,
            "public_harness_repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "public_harness_sha": harness_sha,
            "remote_run": int(ns.run_id),
            "remote_job": int(ns.job_id) if ns.job_id else None,
            "evaluation": ns.evaluation,
            "result_branch": TARGET_BRANCH,
            "result_path": result_rel.as_posix(),
            "files": copied,
        }
        (target_dir / "RUN.json").write_text(
            json.dumps(run_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        git(["add", "--", result_rel.as_posix()], cwd=publish_root, env=env)
        git(
            [
                "-c", "user.name=remote-evaluation-bot",
                "-c", "user.email=remote-evaluation-bot@users.noreply.github.com",
                "commit", "-q", "-m", f"results: {ns.evaluation} run {ns.run_id}",
            ],
            cwd=publish_root,
            env=env,
        )
        commit_sha = git(["rev-parse", "HEAD"], cwd=publish_root, env=env, capture=True)
        if SHA40_RE.fullmatch(commit_sha) is None:
            raise RuntimeError("invalid result commit")
        git(["push", "-q", "origin", f"HEAD:{TARGET_BRANCH}"], cwd=publish_root, env=env)
        print(f"private_result_commit={commit_sha}")
        print(f"private_result_path={result_rel.as_posix()}")
        print("PRIVATE_RESULT_PERSIST_PASS")
        return 0
    except Exception:
        print("PRIVATE_RESULT_PERSIST_FAILED")
        return 91
    finally:
        env.pop("KNOWLEDGE_RESULTS_WRITE_TOKEN", None)
        token = None
        askpass.unlink(missing_ok=True)
        shutil.rmtree(publish_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
