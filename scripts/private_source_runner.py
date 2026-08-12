#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess
import sys


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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source-sha", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--repo", default="xyh-nuage/knowledge")
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

    temp = pathlib.Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    root = temp / "private-source"
    repo_dir = root / "knowledge"
    logs = temp / "private-source-logs"
    root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    env_base = os.environ.copy()
    env_base["GIT_TERMINAL_PROMPT"] = "0"
    run_quiet(["git", "init", "-q", str(repo_dir)], env=env_base)
    run_quiet(["git", "-C", str(repo_dir), "remote", "add", "origin", f"https://github.com/{ns.repo}.git"], env=env_base)

    askpass = temp / "knowledge-askpass.sh"
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
        if proc.returncode != 0:
            print(f"step_{index}=FAIL")
            return proc.returncode or 1
        print(f"step_{index}=PASS")

    print("REMOTE_PRIVATE_SOURCE_CI_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
