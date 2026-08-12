#!/usr/bin/env python3
import argparse, json, os, pathlib, subprocess, sys


def run(args, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source-sha', required=True)
    p.add_argument('--plan', required=True)
    p.add_argument('--repo', default='xyh-nuage/knowledge')
    p.add_argument('--token-env', default='KNOWLEDGE_READ_TOKEN')
    ns = p.parse_args()

    token = os.environ.get(ns.token_env)
    if not token:
        print('PRIVATE_SOURCE_CREDENTIAL_REQUIRED')
        return 86
    if not (len(ns.source_sha) == 40 and all(c in '0123456789abcdef' for c in ns.source_sha.lower())):
        print('INVALID_SOURCE_SHA')
        return 87

    plan_path = pathlib.Path(ns.plan)
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if plan.get('schema') != 'remote_ci_plan.v1':
        print('INVALID_PLAN')
        return 88

    root = pathlib.Path(os.environ.get('RUNNER_TEMP', '/tmp')) / 'private-source'
    root.mkdir(parents=True, exist_ok=True)
    repo_dir = root / 'knowledge'

    # Keep token out of argv/process listing by using git credential input.
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    run(['git', 'init', '-q', str(repo_dir)], env=env)
    run(['git', '-C', str(repo_dir), 'remote', 'add', 'origin', f'https://github.com/{ns.repo}.git'], env=env)

    cred = f'protocol=https\nhost=github.com\nusername=x-access-token\npassword={token}\n\n'
    subprocess.run(['git', 'credential', 'approve'], input=cred, text=True, env=env, check=True)
    try:
        run(['git', '-C', str(repo_dir), 'fetch', '-q', '--depth=1', 'origin', ns.source_sha], env=env)
        run(['git', '-C', str(repo_dir), 'checkout', '-q', '--detach', 'FETCH_HEAD'], env=env)
    finally:
        subprocess.run(['git', 'credential', 'reject'], input=cred, text=True, env=env, check=False)
        token = None

    actual = subprocess.check_output(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != ns.source_sha:
        print('SOURCE_SHA_MISMATCH')
        return 89

    for step in plan.get('steps', []):
        argv = step.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            print('INVALID_PLAN_STEP')
            return 90
        run(argv, cwd=repo_dir, env=env)

    print('REMOTE_PRIVATE_SOURCE_CI_PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
