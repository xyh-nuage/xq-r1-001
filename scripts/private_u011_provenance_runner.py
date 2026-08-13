#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile

TARGET_REPO = "xyh-nuage/knowledge"
FIXTURE_REL = "l1/tests/fixtures/obsolete_media_test_data.zip"
FIXTURE_BLOB = "8782b32680452e3db327b83219b8e9ca737da05c"
INNER = "l0_canonical.db"
TABLE = "canonical_message_versions"


def run(args: list[str], *, cwd: Path | None, env: dict[str, str], capture: bool = False):
    return subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.STDOUT if capture else subprocess.DEVNULL, text=True, check=False)


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
        raise RuntimeError("source SHA mismatch")


def safe_json_value(v):
    if isinstance(v, bytes):
        return {"bytes_hex": v.hex()}
    return v


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-sha", required=True)
    p.add_argument("--stable-key", required=True)
    p.add_argument("--source-version-id", required=True)
    args = p.parse_args()

    token = os.environ.pop("KNOWLEDGE_READ_TOKEN", None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED")
        return 86
    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    root = temp / "private-u011"
    source = root / "source"
    askpass = temp / "private-u011-askpass.sh"
    shutil.rmtree(root, ignore_errors=True)
    askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s\\n" x-access-token ;;\n*Password*) printf "%s\\n" "$KNOWLEDGE_READ_TOKEN" ;;\nesac\n', encoding="utf-8")
    askpass.chmod(0o700)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass)
    env["KNOWLEDGE_READ_TOKEN"] = token
    try:
        fetch_exact(source, args.source_sha.lower(), env)
        archive = source / FIXTURE_REL
        cp = run(["git", "hash-object", str(archive)], cwd=source, env=env, capture=True)
        if cp.returncode or cp.stdout.strip() != FIXTURE_BLOB:
            raise RuntimeError("candidate fixture drift")
        with zipfile.ZipFile(archive) as zf:
            db_bytes = zf.read(INNER)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tmp.write(db_bytes); tmp.flush()
            conn = sqlite3.connect(f"file:{Path(tmp.name).resolve().as_posix()}?mode=ro&immutable=1", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f"SELECT * FROM {TABLE} WHERE stable_key=? AND source_version_id=?",
                                    (args.stable_key, args.source_version_id)).fetchall()
            finally:
                conn.close()
        if len(rows) != 1:
            raise RuntimeError("identity did not resolve exactly once")
        row = {k: safe_json_value(rows[0][k]) for k in rows[0].keys()}
        out = source / ".runtime" / "u011_provenance.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "research_unseen_single_case_provenance.v1",
            "annotation_id": "U011",
            "stable_key": args.stable_key,
            "source_version_id": args.source_version_id,
            "canonical_row": row,
            "neighbor_rows_included": 0,
            "prediction_runs_completed": 0,
        }
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        out.write_bytes(data)
        print("resolved_rows=1")
        print("neighbor_rows_included=0")
        print("prediction_runs_completed=0")
        print("private_payload_sha256=" + hashlib.sha256(data).hexdigest())
        print("U011_PROVENANCE_PRIVATE_PASS")
        return 0
    except Exception:
        print("U011_PROVENANCE_PRIVATE_FAILED")
        return 24
    finally:
        env.pop("KNOWLEDGE_READ_TOKEN", None)
        askpass.unlink(missing_ok=True)

if __name__ == "__main__":
    raise SystemExit(main())
