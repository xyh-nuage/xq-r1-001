#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

SAFE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def safe_path(root: Path, value: str) -> Path:
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("unsafe path")
    resolved = (root / Path(*rel.parts)).resolve()
    if root.resolve() not in resolved.parents:
        raise ValueError("path escapes root")
    return resolved


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def gh_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def gh_json(endpoint: str, token: str) -> dict:
    cp = subprocess.run(["gh", "api", endpoint], env=gh_env(token), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if cp.returncode != 0:
        raise PermissionError("github api denied")
    value = json.loads(cp.stdout.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("unexpected api response")
    return value


def resolve_repo(repo_id: int, token: str) -> str:
    value = gh_json(f"/repositories/{repo_id}", token)
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name or value.get("private") is not True:
        raise ValueError("private repo resolution failed")
    return full_name


def restore_artifacts(plan: dict, *, repo: str, repo_id: int, token: str, source_root: Path, temp: Path) -> None:
    items = plan.get("private_artifacts") or []
    if not isinstance(items, list):
        raise ValueError("invalid artifacts")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("invalid artifact")
        artifact_id = item.get("artifact_id")
        run_id = item.get("expected_run_id")
        digest = str(item.get("expected_digest") or "").lower()
        member = item.get("member")
        destination_value = item.get("destination")
        if not isinstance(artifact_id, int) or artifact_id <= 0 or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("invalid artifact identity")
        if SHA256_RE.fullmatch(digest) is None or not isinstance(member, str) or not isinstance(destination_value, str):
            raise ValueError("invalid artifact declaration")

        metadata = gh_json(f"/repos/{repo}/actions/artifacts/{artifact_id}", token)
        workflow_run = metadata.get("workflow_run") or {}
        if metadata.get("id") != artifact_id or metadata.get("expired") is True:
            raise ValueError("artifact identity mismatch")
        if metadata.get("digest") != f"sha256:{digest}":
            raise ValueError("artifact metadata digest mismatch")
        if workflow_run.get("id") != run_id or workflow_run.get("repository_id") != repo_id:
            raise ValueError("artifact run binding mismatch")
        print(f"private_artifact_{index}_metadata=PASS")

        archive = temp / f"private-artifact-{index}.zip"
        with archive.open("wb") as fh:
            cp = subprocess.run(
                ["gh", "api", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip"],
                env=gh_env(token), stdout=fh, stderr=subprocess.DEVNULL, check=False,
            )
        if cp.returncode != 0:
            raise PermissionError("artifact download denied")
        print(f"private_artifact_{index}_download=PASS")
        if sha256_file(archive) != digest:
            raise ValueError("artifact archive digest mismatch")
        print(f"private_artifact_{index}_digest=PASS")

        rel = PurePosixPath(member)
        if not member or rel.is_absolute() or ".." in rel.parts:
            raise ValueError("unsafe artifact member")
        destination = safe_path(source_root, destination_value)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "r") as zf:
            info = zf.getinfo(member)
            if info.is_dir():
                raise ValueError("artifact member directory")
            destination.write_bytes(zf.read(info))
        archive.unlink(missing_ok=True)
        print(f"private_artifact_{index}_member=PASS")


def json_at(value, path):
    parts = path.split(".") if isinstance(path, str) else path
    if not isinstance(parts, list) or not all(isinstance(part, (str, int)) for part in parts):
        raise ValueError("invalid json path")
    current = value
    for part in parts:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise ValueError("json type mismatch")
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise ValueError("json path missing")
            current = current[part]
    return current


def normalize(value, kind: str) -> str:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid int")
        return str(value)
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid float")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("nonfinite float")
        return format(value, ".12g")
    raise ValueError("invalid metric type")


def audit(index: int, source_root: Path, spec: dict) -> bool:
    try:
        if not isinstance(spec, dict) or spec.get("type") != "nested_numeric_json":
            raise ValueError("unsupported audit")
        report = safe_path(source_root, str(spec.get("path") or ""))
        data = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("report not object")
        for check in spec.get("checks") or []:
            if not isinstance(check, dict) or json_at(data, check.get("path")) != check.get("equals"):
                raise ValueError("invariant failed")
        for check in spec.get("file_checks") or []:
            if not isinstance(check, dict):
                raise ValueError("invalid file check")
            path = safe_path(source_root, str(check.get("path") or ""))
            if not path.is_file() or (check.get("nonempty") is True and path.stat().st_size <= 0):
                raise ValueError("output missing")
            if "line_count" in check:
                expected = check.get("line_count")
                if not isinstance(expected, int):
                    raise ValueError("invalid line count")
                with path.open("r", encoding="utf-8") as fh:
                    if sum(1 for _ in fh) != expected:
                        raise ValueError("line count mismatch")
        rendered: list[tuple[str, str]] = []
        fields = spec.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ValueError("metrics missing")
        for name, declaration in fields.items():
            if not isinstance(name, str) or SAFE_FIELD_RE.fullmatch(name) is None or not isinstance(declaration, dict):
                raise ValueError("invalid metric declaration")
            rendered.append((name, normalize(json_at(data, declaration.get("path")), str(declaration.get("type")))))
        hashes = spec.get("hashes") or {}
        if not isinstance(hashes, dict):
            raise ValueError("hashes invalid")
        for name, path_value in hashes.items():
            if not isinstance(name, str) or SAFE_FIELD_RE.fullmatch(name) is None or not isinstance(path_value, str):
                raise ValueError("hash declaration invalid")
            rendered.append((name, sha256_file(safe_path(source_root, path_value))))
    except Exception:
        print(f"step_{index}_audit=INVALID")
        return False
    for name, value in rendered:
        print(f"step_{index}_metric_{name}={value}")
    print(f"step_{index}_audit=PASS")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--repo-id", default="1307326607")
    ns = parser.parse_args()
    source_sha = ns.source_sha.lower()
    if SHA40_RE.fullmatch(source_sha) is None or not ns.repo_id.isdigit():
        print("INVALID_SOURCE_BINDING")
        return 87
    token = os.environ.pop("KNOWLEDGE_READ_TOKEN", None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED")
        return 86
    try:
        plan = json.loads(Path(ns.plan).read_text(encoding="utf-8"))
    except Exception:
        print("INVALID_PLAN")
        return 88
    if not isinstance(plan, dict) or plan.get("schema") != "remote_eval_plan.v1":
        print("INVALID_PLAN")
        return 88

    repo_id = int(ns.repo_id)
    try:
        repo = resolve_repo(repo_id, token)
    except Exception:
        print("PRIVATE_SOURCE_RESOLUTION_FAILED")
        return 93

    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    source_root = temp / "private-eval-source" / "source"
    logs = temp / "private-eval-logs"
    source_root.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    env_base = os.environ.copy()
    env_base["GIT_TERMINAL_PROMPT"] = "0"
    askpass = temp / "private-eval-askpass.sh"
    askpass.write_text("#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s\\n' x-access-token ;;\n*Password*) printf '%s\\n' \"$KNOWLEDGE_READ_TOKEN\" ;;\nesac\n", encoding="utf-8")
    askpass.chmod(0o700)
    env_auth = env_base.copy()
    env_auth["GIT_ASKPASS"] = str(askpass)
    env_auth["KNOWLEDGE_READ_TOKEN"] = token
    try:
        subprocess.run(["git", "init", "-q", str(source_root)], check=True, env=env_base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source_root), "remote", "add", "origin", f"https://github.com/{repo}.git"], check=True, env=env_base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source_root), "fetch", "-q", "--depth=1", "origin", source_sha], check=True, env=env_auth, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source_root), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True, env=env_auth, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print("PRIVATE_SOURCE_FETCH_FAILED")
        return 91
    finally:
        askpass.unlink(missing_ok=True)
        env_auth.pop("KNOWLEDGE_READ_TOKEN", None)
    actual = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    if actual != source_sha:
        print("SOURCE_SHA_MISMATCH")
        return 89
    print(f"source_sha={actual}")

    try:
        restore_artifacts(plan, repo=repo, repo_id=repo_id, token=token, source_root=source_root, temp=temp)
    except PermissionError:
        print("PRIVATE_ARTIFACT_ACCESS_REQUIRED")
        return 97
    except Exception:
        print("PRIVATE_ARTIFACT_RESTORE_FAILED")
        return 97
    finally:
        token = None

    required_env = plan.get("required_env") or []
    if not isinstance(required_env, list) or any(not isinstance(name, str) for name in required_env):
        print("INVALID_PLAN")
        return 88
    if any(not env_base.get(name) for name in required_env):
        print("MODEL_CREDENTIAL_REQUIRED")
        return 98

    steps = plan.get("steps") or []
    if not isinstance(steps, list) or not steps:
        print("INVALID_PLAN")
        return 88
    for index, step in enumerate(steps, 1):
        argv = step.get("argv") if isinstance(step, dict) else None
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
            print("INVALID_PLAN_STEP")
            return 90
        try:
            cwd = source_root if not step.get("cwd") or step.get("cwd") == "." else safe_path(source_root, str(step.get("cwd")))
        except ValueError:
            print("INVALID_PLAN_CWD")
            return 92
        log_path = logs / f"step-{index}.log"
        with log_path.open("wb") as log:
            cp = subprocess.run(argv, cwd=cwd, env=env_base, stdout=log, stderr=subprocess.STDOUT, shell=False)
        if cp.returncode != 0:
            print(f"step_{index}=FAIL")
            return cp.returncode or 1
        if step.get("audit") is not None and not audit(index, source_root, step["audit"]):
            print(f"step_{index}=FAIL")
            return 96
        print(f"step_{index}=PASS")
    print("REMOTE_PRIVATE_EVAL_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
