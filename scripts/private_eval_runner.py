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
import urllib.error
import urllib.request
import zipfile

SAFE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def run_quiet(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def safe_path(root: Path, value: str) -> Path:
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or ".." in rel.parts:
        raise ValueError("unsafe path")
    resolved = (root / Path(*rel.parts)).resolve()
    if root.resolve() not in resolved.parents:
        raise ValueError("path escapes root")
    return resolved


def safe_cwd(root: Path, value: str | None) -> Path:
    if not value or value == ".":
        return root.resolve()
    return safe_path(root, value)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "remote-private-eval-harness",
    }


def github_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers=api_headers(token))
    with urllib.request.urlopen(req, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("unexpected API response")
    return value


def resolve_private_repo(repo_id: str, token: str) -> str:
    if not repo_id.isdigit():
        raise ValueError("invalid repository id")
    value = github_json(f"https://api.github.com/repositories/{repo_id}", token)
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name or value.get("private") is not True:
        raise ValueError("private repository resolution failed")
    return full_name


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def download_artifact_zip(repo: str, artifact_id: int, token: str, destination: Path) -> None:
    endpoint = f"https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    req = urllib.request.Request(endpoint, headers=api_headers(token))
    opener = urllib.request.build_opener(NoRedirect)
    location: str | None = None
    try:
        with opener.open(req, timeout=30) as response:
            if response.status == 200:
                destination.write_bytes(response.read())
                return
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location")
        elif exc.code in (401, 403, 404):
            raise PermissionError("artifact access denied") from exc
        else:
            raise
    if not location or not location.startswith("https://"):
        raise ValueError("artifact redirect missing")
    # The redirect is a short-lived signed object-store URL. Do not forward the GitHub credential.
    signed_req = urllib.request.Request(location, headers={"User-Agent": "remote-private-eval-harness"})
    with urllib.request.urlopen(signed_req, timeout=60) as response:
        destination.write_bytes(response.read())


def restore_private_artifacts(plan: dict, *, repo: str, repo_id: int, token: str, source_root: Path, temp: Path) -> None:
    artifacts = plan.get("private_artifacts") or []
    if not isinstance(artifacts, list):
        raise ValueError("invalid private_artifacts")
    for index, item in enumerate(artifacts, 1):
        if not isinstance(item, dict):
            raise ValueError("invalid artifact declaration")
        artifact_id = item.get("artifact_id")
        expected_digest = str(item.get("expected_digest") or "").lower()
        expected_run_id = item.get("expected_run_id")
        member = item.get("member")
        destination_value = item.get("destination")
        if not isinstance(artifact_id, int) or artifact_id <= 0:
            raise ValueError("invalid artifact id")
        if SHA256_RE.fullmatch(expected_digest) is None:
            raise ValueError("invalid artifact digest")
        if not isinstance(expected_run_id, int) or expected_run_id <= 0:
            raise ValueError("invalid artifact run")
        if not isinstance(member, str) or not isinstance(destination_value, str):
            raise ValueError("invalid artifact member")

        metadata = github_json(f"https://api.github.com/repos/{repo}/actions/artifacts/{artifact_id}", token)
        if metadata.get("id") != artifact_id or metadata.get("expired") is True:
            raise ValueError("artifact identity invalid")
        if metadata.get("digest") != f"sha256:{expected_digest}":
            raise ValueError("artifact metadata digest mismatch")
        workflow_run = metadata.get("workflow_run") or {}
        if workflow_run.get("id") != expected_run_id or workflow_run.get("repository_id") != repo_id:
            raise ValueError("artifact workflow binding mismatch")

        archive = temp / f"private-artifact-{index}.zip"
        download_artifact_zip(repo, artifact_id, token, archive)
        if sha256_file(archive) != expected_digest:
            raise ValueError("artifact archive digest mismatch")

        rel = PurePosixPath(member)
        if rel.is_absolute() or ".." in rel.parts or not member:
            raise ValueError("unsafe artifact member")
        destination = safe_path(source_root, destination_value)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "r") as zf:
            info = zf.getinfo(member)
            if info.is_dir():
                raise ValueError("artifact member is directory")
            destination.write_bytes(zf.read(info))
        archive.unlink(missing_ok=True)
        print(f"private_artifact_{index}=PASS")


def json_at(value, path):  # noqa: ANN001
    if isinstance(path, str):
        parts = path.split(".") if path else []
    elif isinstance(path, list) and all(isinstance(part, (str, int)) for part in path):
        parts = path
    else:
        raise ValueError("invalid json path")
    current = value
    for part in parts:
        if isinstance(part, int):
            if not isinstance(current, list):
                raise ValueError("json path type mismatch")
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
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite float")
        return format(number, ".12g")
    if kind == "sha256":
        if not isinstance(value, str) or SHA256_RE.fullmatch(value.lower()) is None:
            raise ValueError("invalid sha256")
        return value.lower()
    raise ValueError("unsupported kind")


def audit_step(index: int, source_root: Path, audit: dict) -> bool:
    try:
        if not isinstance(audit, dict) or audit.get("type") != "nested_numeric_json":
            raise ValueError("unsupported audit")
        report_path = safe_path(source_root, str(audit.get("path") or ""))
        data = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("audit report not object")

        checks = audit.get("checks") or []
        if not isinstance(checks, list):
            raise ValueError("invalid checks")
        for check in checks:
            if not isinstance(check, dict) or "equals" not in check:
                raise ValueError("invalid check")
            if json_at(data, check.get("path")) != check.get("equals"):
                raise ValueError("audit invariant failed")

        file_checks = audit.get("file_checks") or []
        if not isinstance(file_checks, list):
            raise ValueError("invalid file checks")
        for check in file_checks:
            if not isinstance(check, dict):
                raise ValueError("invalid file check")
            path = safe_path(source_root, str(check.get("path") or ""))
            if not path.is_file():
                raise ValueError("required output missing")
            if check.get("nonempty") is True and path.stat().st_size <= 0:
                raise ValueError("required output empty")
            if "line_count" in check:
                expected = check["line_count"]
                if not isinstance(expected, int) or expected < 0:
                    raise ValueError("invalid line count")
                with path.open("r", encoding="utf-8", errors="strict") as fh:
                    actual = sum(1 for _ in fh)
                if actual != expected:
                    raise ValueError("line count mismatch")

        rendered: list[tuple[str, str]] = []
        fields = audit.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ValueError("invalid fields")
        for name, declaration in fields.items():
            if not isinstance(name, str) or SAFE_FIELD_RE.fullmatch(name) is None or not isinstance(declaration, dict):
                raise ValueError("invalid field declaration")
            kind = declaration.get("type")
            rendered.append((name, normalize(json_at(data, declaration.get("path")), str(kind))))

        hashes = audit.get("hashes") or {}
        if not isinstance(hashes, dict):
            raise ValueError("invalid hashes")
        for name, path_value in hashes.items():
            if not isinstance(name, str) or SAFE_FIELD_RE.fullmatch(name) is None or not isinstance(path_value, str):
                raise ValueError("invalid hash declaration")
            rendered.append((name, sha256_file(safe_path(source_root, path_value))))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, ValueError, zipfile.BadZipFile):
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
    parser.add_argument("--token-env", default="KNOWLEDGE_READ_TOKEN")
    ns = parser.parse_args()

    source_sha = ns.source_sha.lower()
    if SHA40_RE.fullmatch(source_sha) is None:
        print("INVALID_SOURCE_SHA")
        return 87
    token = os.environ.pop(ns.token_env, None)
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

    repo_id = int(ns.repo_id) if ns.repo_id.isdigit() else -1
    if repo_id <= 0:
        print("PRIVATE_SOURCE_RESOLUTION_FAILED")
        return 93
    try:
        private_repo = resolve_private_repo(ns.repo_id, token)
    except Exception:
        print("PRIVATE_SOURCE_RESOLUTION_FAILED")
        return 93

    temp = Path(os.environ.get("RUNNER_TEMP", "/tmp"))
    root = temp / "private-eval-source"
    source_root = root / "source"
    logs = temp / "private-eval-logs"
    root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    env_base = os.environ.copy()
    env_base["GIT_TERMINAL_PROMPT"] = "0"

    askpass = temp / "private-eval-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in\n  *Username*) printf '%s\\n' x-access-token ;;\n  *Password*) printf '%s\\n' \"$KNOWLEDGE_READ_TOKEN\" ;;\nesac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    env_auth = env_base.copy()
    env_auth["GIT_ASKPASS"] = str(askpass)
    env_auth["KNOWLEDGE_READ_TOKEN"] = token

    try:
        run_quiet(["git", "init", "-q", str(source_root)], env=env_base)
        run_quiet(["git", "-C", str(source_root), "remote", "add", "origin", f"https://github.com/{private_repo}.git"], env=env_base)
        run_quiet(["git", "-C", str(source_root), "fetch", "-q", "--depth=1", "origin", source_sha], env=env_auth)
        run_quiet(["git", "-C", str(source_root), "checkout", "-q", "--detach", "FETCH_HEAD"], env=env_auth)
    except subprocess.CalledProcessError:
        print("PRIVATE_SOURCE_FETCH_FAILED")
        return 91
    finally:
        env_auth.pop("KNOWLEDGE_READ_TOKEN", None)
        askpass.unlink(missing_ok=True)

    actual = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    if actual != source_sha:
        print("SOURCE_SHA_MISMATCH")
        return 89
    print(f"source_sha={actual}")

    try:
        restore_private_artifacts(plan, repo=private_repo, repo_id=repo_id, token=token, source_root=source_root, temp=temp)
    except PermissionError:
        print("PRIVATE_ARTIFACT_ACCESS_REQUIRED")
        return 97
    except (OSError, urllib.error.URLError, ValueError, KeyError, zipfile.BadZipFile):
        print("PRIVATE_ARTIFACT_RESTORE_FAILED")
        return 97
    finally:
        token = None

    required_env = plan.get("required_env") or []
    if not isinstance(required_env, list) or not all(isinstance(name, str) and name for name in required_env):
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
        if not isinstance(step, dict):
            print("INVALID_PLAN_STEP")
            return 90
        argv = step.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
            print("INVALID_PLAN_STEP")
            return 90
        try:
            cwd = safe_cwd(source_root, step.get("cwd"))
        except ValueError:
            print("INVALID_PLAN_CWD")
            return 92
        log_path = logs / f"step-{index}.log"
        with log_path.open("wb") as log:
            proc = subprocess.run(argv, cwd=cwd, env=env_base, stdout=log, stderr=subprocess.STDOUT, shell=False)
        if proc.returncode != 0:
            print(f"step_{index}=FAIL")
            return proc.returncode or 1
        audit = step.get("audit")
        if audit is not None and not audit_step(index, source_root, audit):
            print(f"step_{index}=FAIL")
            return 96
        print(f"step_{index}=PASS")

    print("REMOTE_PRIVATE_EVAL_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
