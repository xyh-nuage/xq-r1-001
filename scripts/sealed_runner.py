#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HEX64 = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def fail(code: str, rc: int = 2) -> None:
    print(f"sealed_ci={code}")
    raise SystemExit(rc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode("ascii"))
    h.update(data)
    return h.hexdigest()


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        fail("INVALID_JSON")


def safe_rel(value: str) -> Path:
    p = Path(value)
    if not value or p.is_absolute() or ".." in p.parts:
        fail("UNSAFE_PATH")
    return p


def verify_binding(args) -> None:
    binding = load_json(Path(args.binding))
    if binding.get("schema") != "sealed_binding.v2":
        fail("BINDING_SCHEMA_MISMATCH")
    if binding.get("target_id") != args.target:
        fail("TARGET_MISMATCH")
    declared = binding.get("ciphertext_sha256", "")
    archive_hash = binding.get("plaintext_archive_sha256", "")
    if not HEX64.fullmatch(declared) or not HEX64.fullmatch(archive_hash):
        fail("BINDING_HASH_INVALID")
    actual = sha256_file(Path(args.ciphertext))
    if actual != declared or actual != args.expected_ciphertext:
        fail("CIPHERTEXT_HASH_MISMATCH")
    print("binding=PASS")


def verify_archive(args) -> None:
    binding = load_json(Path(args.binding))
    actual = sha256_file(Path(args.archive))
    if actual != binding.get("plaintext_archive_sha256"):
        fail("ARCHIVE_HASH_MISMATCH")
    print("archive=PASS")


def extract_archive(args) -> None:
    archive = Path(args.archive)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tf:
            members = tf.getmembers()
            for m in members:
                p = Path(m.name)
                if not m.name or p.is_absolute() or ".." in p.parts:
                    fail("UNSAFE_ARCHIVE_MEMBER")
                if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                    fail("UNSAFE_ARCHIVE_TYPE")
            tf.extractall(dest, filter="data")
    except SystemExit:
        raise
    except Exception:
        fail("ARCHIVE_EXTRACT_FAILED")
    print("extract=PASS")


def expected_mode(mode_value) -> int:
    text = str(mode_value)
    if text in {"100644", "0o100644"}:
        return 0o644
    if text in {"100755", "0o100755"}:
        return 0o755
    fail("MANIFEST_MODE_INVALID")


def verify_manifest(root: Path) -> None:
    manifest_path = root / "SOURCE_MANIFEST.json"
    manifest = load_json(manifest_path)
    if manifest.get("schema") != "sealed_source_manifest.v1":
        fail("MANIFEST_SCHEMA_MISMATCH")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        fail("MANIFEST_EMPTY")
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            fail("MANIFEST_ENTRY_INVALID")
        rel = safe_rel(str(item.get("path", "")))
        if str(rel) in seen:
            fail("MANIFEST_DUPLICATE_PATH")
        seen.add(str(rel))
        path = root / rel
        try:
            st = path.lstat()
        except OSError:
            fail("MANIFEST_FILE_MISSING")
        if not stat.S_ISREG(st.st_mode):
            fail("MANIFEST_NONREGULAR_FILE")
        expected_sha = str(item.get("sha256", ""))
        expected_blob = str(item.get("git_blob_sha1", ""))
        if not HEX64.fullmatch(expected_sha):
            fail("MANIFEST_SHA_INVALID")
        if not re.fullmatch(r"[0-9a-f]{40}", expected_blob):
            fail("MANIFEST_BLOB_INVALID")
        if sha256_file(path) != expected_sha:
            fail("MANIFEST_SHA_MISMATCH")
        if git_blob_sha1(path) != expected_blob:
            fail("MANIFEST_BLOB_MISMATCH")
        mode = stat.S_IMODE(st.st_mode)
        want = expected_mode(item.get("mode"))
        if mode != want:
            try:
                path.chmod(want)
            except OSError:
                fail("MANIFEST_MODE_MISMATCH")
    print("manifest=PASS")


def junit_counts(path: Path):
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        tests = sum(int(x.attrib.get("tests", 0)) for x in suites)
        failures = sum(int(x.attrib.get("failures", 0)) for x in suites)
        errors = sum(int(x.attrib.get("errors", 0)) for x in suites)
        skipped = sum(int(x.attrib.get("skipped", 0)) for x in suites)
        return tests, failures, errors, skipped
    except Exception:
        return None


def run_plan(args) -> None:
    root = Path(args.root).resolve()
    verify_manifest(root)
    plan = load_json(root / "CI_PLAN.json")
    if plan.get("schema") != "sealed_ci_plan.v1":
        fail("PLAN_SCHEMA_MISMATCH")
    if plan.get("target_id") != args.target:
        fail("PLAN_TARGET_MISMATCH")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        fail("PLAN_EMPTY")
    log_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "sealed-logs"
    log_root.mkdir(parents=True, exist_ok=True)
    overall = 0
    for idx, op in enumerate(operations):
        if not isinstance(op, dict):
            fail("PLAN_OPERATION_INVALID")
        op_id = str(op.get("id", ""))
        if not ID_RE.fullmatch(op_id):
            fail("PLAN_OPERATION_ID_INVALID")
        argv = op.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            fail("PLAN_ARGV_INVALID")
        cwd_rel = safe_rel(str(op.get("cwd", "."))) if op.get("cwd", ".") != "." else Path(".")
        cwd = (root / cwd_rel).resolve()
        if root != cwd and root not in cwd.parents:
            fail("PLAN_CWD_INVALID")
        if not cwd.is_dir():
            fail("PLAN_CWD_MISSING")
        timeout = int(op.get("timeout_seconds", 1800))
        if timeout < 1 or timeout > 7200:
            fail("PLAN_TIMEOUT_INVALID")
        env = os.environ.copy()
        static_env = op.get("env", {})
        if not isinstance(static_env, dict):
            fail("PLAN_ENV_INVALID")
        for key, value in static_env.items():
            if not ID_RE.fullmatch(str(key)) or not isinstance(value, str):
                fail("PLAN_ENV_INVALID")
            env[str(key)] = value
        secret_slots = op.get("secret_slots", {})
        if not isinstance(secret_slots, dict):
            fail("PLAN_SECRET_SLOT_INVALID")
        for target_name, slot in secret_slots.items():
            if not ID_RE.fullmatch(str(target_name)) or not isinstance(slot, int) or slot < 1 or slot > 8:
                fail("PLAN_SECRET_SLOT_INVALID")
            value = os.environ.get(f"SEALED_SECRET_{slot}")
            if not value:
                fail("PLAN_SECRET_MISSING")
            env[str(target_name)] = value
        log_path = log_root / f"op-{idx:02d}.log"
        started = time.monotonic()
        try:
            with log_path.open("wb") as log:
                cp = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    shell=False, timeout=timeout, check=False)
            rc = cp.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        elapsed = int(time.monotonic() - started)
        status_text = "PASS" if rc == 0 else "FAIL"
        summary = f"op={op_id} status={status_text} seconds={elapsed}"
        junit_rel = op.get("junit_xml")
        if isinstance(junit_rel, str) and junit_rel:
            jpath = root / safe_rel(junit_rel)
            counts = junit_counts(jpath)
            if counts is not None:
                summary += f" tests={counts[0]} failures={counts[1]} errors={counts[2]} skipped={counts[3]}"
        print(summary)
        if rc != 0:
            overall = rc if 0 < rc < 256 else 1
            if not op.get("continue_on_error", False):
                break
    raise SystemExit(overall)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify-binding")
    p.add_argument("--binding", required=True)
    p.add_argument("--ciphertext", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--expected-ciphertext", required=True)
    p.set_defaults(func=verify_binding)

    p = sub.add_parser("verify-archive")
    p.add_argument("--binding", required=True)
    p.add_argument("--archive", required=True)
    p.set_defaults(func=verify_archive)

    p = sub.add_parser("extract")
    p.add_argument("--archive", required=True)
    p.add_argument("--dest", required=True)
    p.set_defaults(func=extract_archive)

    p = sub.add_parser("run")
    p.add_argument("--root", required=True)
    p.add_argument("--target", required=True)
    p.set_defaults(func=run_plan)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
