#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

from private_eval_runner_v2 import (
    SHA40_RE,
    audit,
    resolve_repo,
    restore_artifacts,
    safe_path,
    sha256_file,
)


def expand_arg(value: str, harness_root: Path) -> str:
    return value.replace("{{HARNESS_ROOT}}", str(harness_root))


def encrypt_bundle(
    spec: dict,
    *,
    source_root: Path,
    harness_root: Path,
    temp: Path,
    source_sha: str,
) -> None:
    if not isinstance(spec, dict):
        raise ValueError("invalid encrypted bundle")
    files = spec.get("files") or []
    public_key_value = spec.get("public_key")
    if not isinstance(files, list) or not files or not all(isinstance(value, str) and value for value in files):
        raise ValueError("invalid encrypted bundle files")
    if not isinstance(public_key_value, str) or not public_key_value:
        raise ValueError("invalid public key")

    public_key = safe_path(harness_root, public_key_value)
    if not public_key.is_file():
        raise ValueError("public key missing")

    plain_zip = temp / "shared-private-diagnostic.zip"
    with zipfile.ZipFile(plain_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for value in files:
            path = safe_path(source_root, value)
            if not path.is_file():
                raise ValueError(f"bundle file missing: {value}")
            zf.write(path, arcname=value)

    output_dir = temp / "encrypted-private-output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    passfile = temp / "shared-private-diagnostic.pass"
    encrypted_key = output_dir / "key.enc"
    encrypted_payload = output_dir / "payload.enc"

    with passfile.open("wb") as fh:
        subprocess.run(
            ["openssl", "rand", "-base64", "48"],
            check=True,
            stdout=fh,
            stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-encrypt",
            "-pubin",
            "-inkey",
            str(public_key),
            "-pkeyopt",
            "rsa_padding_mode:oaep",
            "-pkeyopt",
            "rsa_oaep_md:sha256",
            "-in",
            str(passfile),
            "-out",
            str(encrypted_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-salt",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(plain_zip),
            "-out",
            str(encrypted_payload),
            "-pass",
            f"file:{passfile}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    metadata = {
        "schema": "encrypted_private_diagnostic.v1",
        "source_sha": source_sha,
        "key_encryption": "RSA-OAEP-SHA256",
        "payload_encryption": "AES-256-CBC-PBKDF2-SHA256-iter200000",
        "public_key_sha256": sha256_file(public_key),
        "plaintext_zip_sha256": sha256_file(plain_zip),
        "encrypted_key_sha256": sha256_file(encrypted_key),
        "encrypted_payload_sha256": sha256_file(encrypted_payload),
        "files": files,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    passfile.unlink(missing_ok=True)
    plain_zip.unlink(missing_ok=True)
    print("encrypted_bundle=PASS")
    print(f"encrypted_payload_sha256={metadata['encrypted_payload_sha256']}")


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
    plan_path = Path(ns.plan).resolve()
    harness_root = plan_path.parent.parent
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
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
    askpass.write_text(
        "#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s\\n' x-access-token ;;\n*Password*) printf '%s\\n' \"$KNOWLEDGE_READ_TOKEN\" ;;\nesac\n",
        encoding="utf-8",
    )
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
        argv = [expand_arg(value, harness_root) for value in argv]
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

    try:
        encrypt_bundle(
            plan.get("encrypted_bundle"),
            source_root=source_root,
            harness_root=harness_root,
            temp=temp,
            source_sha=source_sha,
        )
    except Exception:
        print("ENCRYPTED_BUNDLE_FAILED")
        return 99

    print("REMOTE_PRIVATE_EVAL_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
