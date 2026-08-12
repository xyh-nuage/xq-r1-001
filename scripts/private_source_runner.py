#!/usr/bin/env python3
import argparse
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import urllib.request


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SAFE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def run_quiet(args, *, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def safe_cwd(root: pathlib.Path, value: str | None) -> pathlib.Path:
    rel = pathlib.PurePosixPath(value or ".")
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("invalid cwd")
    resolved = (root / pathlib.Path(*rel.parts)).resolve()
    if root.resolve() not in (resolved, *resolved.parents):
        raise ValueError("cwd escapes source root")
    return resolved


def safe_path(root: pathlib.Path, value: str) -> pathlib.Path:
    rel = pathlib.PurePosixPath(value)
    if not value or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("invalid path")
    resolved = (root / pathlib.Path(*rel.parts)).resolve()
    if root.resolve() not in resolved.parents:
        raise ValueError("path escapes source root")
    return resolved


def resolve_private_repo(repo_id: str, token: str) -> str:
    if not repo_id.isdigit():
        raise ValueError("invalid repository id")
    req = urllib.request.Request(
        f"https://api.github.com/repositories/{repo_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "remote-ci-harness",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    full_name = data.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name or data.get("private") is not True:
        raise ValueError("private repository resolution failed")
    return full_name


def is_pytest_step(argv: list[str]) -> bool:
    return "pytest" in argv or any(part.endswith("/pytest") for part in argv)


def parse_pytest_summary(log_path: pathlib.Path) -> dict[str, int] | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()[-80:]
    count_pattern = re.compile(
        r"(?P<count>\d+)\s+(?P<name>passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?)\b"
    )
    for raw_line in reversed(lines):
        line = ANSI_RE.sub("", raw_line).strip()
        matches = list(count_pattern.finditer(line))
        if not matches:
            continue
        if " in " not in line and not line.startswith("="):
            continue
        result = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "deselected": 0,
            "errors": 0,
        }
        for match in matches:
            name = match.group("name")
            if name in ("warning", "warnings"):
                continue
            if name == "error":
                name = "errors"
            result[name] += int(match.group("count"))
        if any(result.values()):
            return result
    return None


def print_pytest_summary(index: int, log_path: pathlib.Path) -> bool:
    summary = parse_pytest_summary(log_path)
    if summary is None:
        print(f"step_{index}_pytest_summary=UNAVAILABLE")
        return False
    ordered = ("passed", "failed", "skipped", "xfailed", "xpassed", "deselected", "errors")
    payload = ",".join(f"{name}:{summary[name]}" for name in ordered)
    print(f"step_{index}_pytest_summary={payload}")
    return True


def normalize_metric(value, kind: str) -> str:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid int metric")
        return str(value)
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid float metric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite metric")
        return format(number, ".12g")
    if kind == "sha256":
        if not isinstance(value, str) or SHA256_RE.fullmatch(value.lower()) is None:
            raise ValueError("invalid sha256 metric")
        return value.lower()
    raise ValueError("unsupported metric type")


def print_numeric_json_audit(index: int, repo_dir: pathlib.Path, audit) -> bool:
    if not isinstance(audit, dict) or audit.get("type") != "numeric_json":
        print(f"step_{index}_audit=INVALID")
        return False
    path_value = audit.get("path")
    fields = audit.get("fields")
    if not isinstance(path_value, str) or not isinstance(fields, dict) or not fields:
        print(f"step_{index}_audit=INVALID")
        return False
    try:
        report_path = safe_path(repo_dir, path_value)
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("report is not object")
        rendered = []
        for name, kind in fields.items():
            if not isinstance(name, str) or SAFE_FIELD_RE.fullmatch(name) is None:
                raise ValueError("unsafe metric name")
            if kind not in ("int", "float", "sha256") or name not in data:
                raise ValueError("invalid metric declaration")
            rendered.append((name, normalize_metric(data[name], kind)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        print(f"step_{index}_audit=INVALID")
        return False

    for name, value in rendered:
        print(f"step_{index}_metric_{name}={value}")
    print(f"step_{index}_audit=PASS")
    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-sha", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--repo-id", default="1307326607")
    p.add_argument("--token-env", default="KNOWLEDGE_READ_TOKEN")
    ns = p.parse_args()

    token = os.environ.pop(ns.token_env, None)
    if not token:
        print("PRIVATE_SOURCE_CREDENTIAL_REQUIRED")
        return 86
    if not (len(ns.source_sha) == 40 and all(c in "0123456789abcdef" for c in ns.source_sha.lower())):
        print("INVALID_SOURCE_SHA")
        return 87

    plan = json.loads(pathlib.Path(ns.plan).read_text(encoding="utf-8"))
    if plan.get("schema") != "remote_ci_plan.v1":
        print("INVALID_PLAN")
        return 88

    try:
        private_repo = resolve_private_repo(ns.repo_id, token)
    except Exception:
        print("PRIVATE_SOURCE_RESOLUTION_FAILED")
        return 93

    temp = pathlib.Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    root = temp / "private-source"
    repo_dir = root / "source"
    logs = temp / "private-source-logs"
    root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    env_base = os.environ.copy()
    env_base["GIT_TERMINAL_PROMPT"] = "0"
    run_quiet(["git", "init", "-q", str(repo_dir)], env=env_base)
    run_quiet(["git", "-C", str(repo_dir), "remote", "add", "origin", f"https://github.com/{private_repo}.git"], env=env_base)

    askpass = temp / "private-source-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in\n  *Username*) printf '%s\\n' x-access-token ;;\n  *Password*) printf '%s\\n' \"$KNOWLEDGE_READ_TOKEN\" ;;\nesac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env_auth = env_base.copy()
    env_auth["GIT_ASKPASS"] = str(askpass)
    env_auth["KNOWLEDGE_READ_TOKEN"] = token
    try:
        run_quiet(["git", "-C", str(repo_dir), "fetch", "-q", "--depth=1", "origin", ns.source_sha], env=env_auth)
        run_quiet(["git", "-C", str(repo_dir), "checkout", "-q", "--detach", "FETCH_HEAD"], env=env_auth)
    except subprocess.CalledProcessError:
        print("PRIVATE_SOURCE_FETCH_FAILED")
        return 91
    finally:
        token = None
        env_auth.pop("KNOWLEDGE_READ_TOKEN", None)
        try:
            askpass.unlink()
        except FileNotFoundError:
            pass

    actual = subprocess.check_output(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], text=True).strip()
    if actual != ns.source_sha:
        print("SOURCE_SHA_MISMATCH")
        return 89

    print(f"source_sha={actual}")
    for index, step in enumerate(plan.get("steps", []), start=1):
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            print("INVALID_PLAN_STEP")
            return 90
        try:
            cwd = safe_cwd(repo_dir, step.get("cwd"))
        except ValueError:
            print("INVALID_PLAN_CWD")
            return 92
        log_path = logs / f"step-{index}.log"
        with log_path.open("wb") as fh:
            proc = subprocess.run(argv, cwd=cwd, env=env_base, stdout=fh, stderr=subprocess.STDOUT)

        if is_pytest_step(argv):
            if not print_pytest_summary(index, log_path):
                print(f"step_{index}=FAIL")
                return 95

        if proc.returncode != 0:
            print(f"step_{index}=FAIL")
            return proc.returncode or 1

        audit = step.get("audit")
        if audit is not None and not print_numeric_json_audit(index, repo_dir, audit):
            print(f"step_{index}=FAIL")
            return 96

        print(f"step_{index}=PASS")

    print("REMOTE_PRIVATE_SOURCE_CI_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
