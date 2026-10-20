#!/usr/bin/env python3
"""Dispatch public CI and discover correlated metadata; never wait for a build.

Uses gh's inherited authentication without inspecting it. No local Git operations
or persistent request state: status reports the run's observed commit, whereas
submit also verifies it against the commit resolved before dispatch.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote
import uuid


FIELDS = 'databaseId,displayTitle,headSha,status,conclusion,url'
DISCOVERY_SECONDS = 5.0


class CIError(Exception):
    pass


class GhTimeout(CIError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal diagnostics echo untrusted argument values.
        raise CIError('invalid_arguments')


def repo_value(value):
    parts = value.split('/')
    if (len(parts) != 2
            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', parts[0])
            or not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}', parts[1])
            or parts[1] in ('.', '..')):
        raise CIError('invalid_repo')
    return value


def ref_value(value):
    # Branch-style refs, including slash-separated names, but no option, URL,
    # revision-expression or control-character injection.
    if (not value or value.startswith(('-', '/')) or value.endswith(('/', '.'))
            or value == '@' or '..' in value or '@{' in value
            or any(c.isspace() or not c.isprintable() for c in value)
            or any(c in value for c in '~^:?*[\\')
            or any(not part or part.startswith('.') or part.endswith('.lock')
                   for part in value.split('/'))):
        raise CIError('invalid_ref')
    return value


def request_value(value):
    if not re.fullmatch(r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', value):
        raise CIError('invalid_request')
    return uuid.UUID(value).hex


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value)


def gh(args, *, operation, timeout=15, payload=None):
    try:
        result = subprocess.run(['gh', *args],
                                input=json.dumps(payload) if payload is not None else None,
                                stdin=subprocess.DEVNULL if payload is None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise GhTimeout(operation + '_timeout') from None
    except (OSError, UnicodeError):
        raise CIError(operation + '_failed') from None
    if result.returncode:
        raise CIError(operation + '_failed')
    return result.stdout


def discover(repo, request, timeout):
    raw = gh(['run', 'list', '--repo', repo, '--workflow', 'kernel.yml',
              '--event', 'workflow_dispatch', '--json', FIELDS, '--limit', '30'],
             operation='discovery', timeout=timeout)
    try:
        runs = json.loads(raw)
    except ValueError:
        raise CIError('invalid_run_metadata') from None
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise CIError('invalid_run_metadata')
    return next((run for run in runs if run.get('displayTitle') == 'Kernel lab ' + request), None)


def record_run(result, run, expected=None):
    commit = run.get('headSha')
    if (not valid_sha(commit) or type(run.get('databaseId')) is not int
            or run['databaseId'] <= 0 or not isinstance(run.get('url'), str)
            or not isinstance(run.get('status'), str)
            or (run.get('conclusion') is not None and not isinstance(run['conclusion'], str))):
        raise CIError('invalid_run_metadata')
    result.update(run_id=run['databaseId'], url=run['url'])
    if expected is not None and commit != expected:
        result['observed_commit'] = commit
        raise CIError('commit_mismatch')
    result.update(commit=commit, status=run['status'], conclusion=run.get('conclusion'))


def submit(args, result):
    commit = gh(['api', f'repos/{args.repo}/commits/{quote(args.ref, safe="")}', '--jq', '.sha'],
                operation='resolve').strip()
    if not valid_sha(commit):
        raise CIError('invalid_commit')
    result.update(request_id=uuid.uuid4().hex, commit=commit)
    gh(['api', f'repos/{args.repo}/actions/workflows/kernel.yml/dispatches',
        '--method', 'POST', '--input', '-'], operation='dispatch',
       payload={'ref': args.ref, 'inputs': {'mode': args.mode,
                'request_id': result['request_id'], 'expected_sha': commit}})
    # Bound metadata discovery only, not compilation. Even in_progress returns
    # immediately; a slow query exhausts this budget rather than adding retries.
    deadline = time.monotonic() + DISCOVERY_SECONDS
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            run = discover(args.repo, result['request_id'], remaining)
        except GhTimeout:
            break
        if run is not None:
            record_run(result, run, expected=commit)
            return
        if attempt < 2:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    result['status'] = 'pending_discovery'


def main(argv=None):
    result = {}
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        dispatch = commands.add_parser('submit', help='dispatch without waiting for compilation')
        dispatch.add_argument('--repo', required=True, type=repo_value)
        dispatch.add_argument('--ref', default='main', type=ref_value)
        dispatch.add_argument('--mode', choices=('incremental', 'clean'), default='incremental')
        status = commands.add_parser('status', help='query correlated run metadata once')
        status.add_argument('--repo', required=True, type=repo_value)
        status.add_argument('--request', required=True, type=request_value)
        args = parser.parse_args(argv)
        if args.command == 'submit':
            submit(args, result)
        else:
            result['request_id'] = args.request
            run = discover(args.repo, args.request, DISCOVERY_SECONDS)
            if run is None:
                result['status'] = 'pending_discovery'
            else:
                record_run(result, run)
    except CIError as error:
        result.update(status='error', error=str(error))
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
