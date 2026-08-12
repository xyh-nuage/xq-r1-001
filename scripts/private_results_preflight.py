#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

TARGET_REPO = "xyh-nuage/knowledge"
TARGET_BRANCH = "evaluation-results"


def run(args: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    cp = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError("preflight command failed")


def main() -> int:
    token = os.environ.pop("KNOWLEDGE_RESULTS_WRITE_TOKEN", None)
    if not token:
        print("PRIVATE_RESULTS_WRITE_CREDENTIAL_REQUIRED")
        return 86

    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp")).resolve()
    root = temp / "private-results-preflight"
    askpass = temp / "private-results-preflight-askpass.sh"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
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
        run(["git", "init", "-q"], cwd=root, env=env)
        run(["git", "remote", "add", "origin", f"https://github.com/{TARGET_REPO}.git"], cwd=root, env=env)
        run(["git", "fetch", "-q", "--depth=1", "origin", TARGET_BRANCH], cwd=root, env=env)
        run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=root, env=env)
        run(["git", "config", "user.name", "remote-results-preflight"], cwd=root, env=env)
        run(["git", "config", "user.email", "remote-results-preflight@users.noreply.github.com"], cwd=root, env=env)
        (root / ".results-write-preflight").write_text("probe\n", encoding="utf-8")
        run(["git", "add", "--", ".results-write-preflight"], cwd=root, env=env)
        run(["git", "commit", "-q", "-m", "dry-run result writer preflight"], cwd=root, env=env)
        run(["git", "push", "--dry-run", "-q", "origin", f"HEAD:refs/heads/{TARGET_BRANCH}"], cwd=root, env=env)
        print("PRIVATE_RESULTS_WRITE_PREFLIGHT_PASS")
        return 0
    except Exception:
        print("PRIVATE_RESULTS_WRITE_PREFLIGHT_FAILED")
        return 87
    finally:
        env.pop("KNOWLEDGE_RESULTS_WRITE_TOKEN", None)
        token = None
        askpass.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
