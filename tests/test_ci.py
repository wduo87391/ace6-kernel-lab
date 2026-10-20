"""Offline CLI tests; fake gh never contacts GitHub or waits for a build."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


DRIVER = Path(__file__).resolve().parents[1] / 'tools/ci.py'
REPO = 'wduo87391/ace6-kernel-lab'
SHA = 'a' * 40
REQUEST = '12345678123442348234123456789abc'
FAKE_GH = r'''import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_GH_ROOT'])
args = sys.argv[1:]
with (root / 'calls').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
scenario = json.loads((root / 'scenario').read_text())
if args[:1] == ['api'] and 'commits/' in args[1]:
    if scenario.get('resolve_fail'):
        print('secret credential ghp_DO_NOT_LEAK', file=sys.stderr)
        sys.exit(1)
    print(scenario.get('sha', 'a' * 40))
elif args[:1] == ['api'] and args[1].endswith('/dispatches'):
    payload = json.load(sys.stdin)
    (root / 'payload').write_text(json.dumps(payload))
    if scenario.get('dispatch_fail'):
        print('secret credential ghp_DO_NOT_LEAK', file=sys.stderr)
        sys.exit(1)
elif args[:2] == ['run', 'list']:
    if scenario.get('discovery_fail'):
        print('secret credential ghp_DO_NOT_LEAK', file=sys.stderr)
        sys.exit(1)
    if scenario.get('malformed'):
        print('secret credential ghp_DO_NOT_LEAK')
        sys.exit(0)
    if scenario.get('slow'):
        import time
        time.sleep(10)
    count_file = root / 'queries'
    count = int(count_file.read_text()) if count_file.exists() else 0
    count_file.write_text(str(count + 1))
    request = scenario.get('request')
    if (root / 'payload').exists():
        request = json.loads((root / 'payload').read_text())['inputs']['request_id']
    run = {'databaseId': 42, 'displayTitle': 'Kernel lab ' + request,
           'headSha': scenario.get('head_sha', 'a' * 40),
           'status': scenario.get('status', 'in_progress'),
           'conclusion': scenario.get('conclusion'),
           'url': 'https://github.com/wduo87391/ace6-kernel-lab/actions/runs/42'}
    unrelated = dict(run, databaseId=99, displayTitle=run['displayTitle'] + ' extra')
    print(json.dumps([unrelated] if count < scenario.get('delay', 0)
                     else [unrelated, run]))
else:
    print('unexpected gh command', file=sys.stderr)
    sys.exit(9)
'''


class CITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kernel-lab-ci-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        gh = self.root / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + FAKE_GH)
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        FAKE_GH_ROOT=str(self.root), PYTHONDONTWRITEBYTECODE='1')
        self.scenario()

    def scenario(self, **values):
        (self.root / 'scenario').write_text(json.dumps(values))

    def calls(self):
        path = self.root / 'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(DRIVER), *args], env=self.env,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        self.assertNotIn('ghp_DO_NOT_LEAK', result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_submit_resolves_dispatches_and_discovers_exact_request_without_wait(self):
        self.scenario(delay=1)
        start = time.monotonic()
        result = self.cli('submit', '--repo', REPO)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result['commit'], SHA)
        self.assertEqual(result['run_id'], 42)
        self.assertEqual(result['status'], 'in_progress')
        self.assertIsNone(result['conclusion'])
        request = result['request_id']
        self.assertEqual(uuid.UUID(hex=request).hex, request)
        payload = json.loads((self.root / 'payload').read_text())
        self.assertEqual(payload, {'ref': 'main', 'inputs': {
            'mode': 'incremental', 'request_id': request, 'expected_sha': SHA}})
        calls = self.calls()
        self.assertEqual(calls[0], ['api', f'repos/{REPO}/commits/main', '--jq', '.sha'])
        self.assertEqual(calls[1], ['api', f'repos/{REPO}/actions/workflows/kernel.yml/dispatches',
                                    '--method', 'POST', '--input', '-'])
        expected_list = ['run', 'list', '--repo', REPO, '--workflow', 'kernel.yml',
                         '--event', 'workflow_dispatch', '--json',
                         'databaseId,displayTitle,headSha,status,conclusion,url', '--limit', '30']
        self.assertEqual(calls[2:], [expected_list, expected_list])

    def test_submit_pending_is_bounded_and_does_not_claim_build_success(self):
        self.scenario(delay=20)
        start = time.monotonic()
        result = self.cli('submit', '--repo', REPO)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result, {'request_id': result['request_id'], 'commit': SHA,
                                  'status': 'pending_discovery'})
        self.assertEqual(len(self.calls()), 5)  # resolve + dispatch + three metadata queries
        self.assertEqual((self.root / 'queries').read_text(), '3')

    def test_moved_branch_fails_closed_with_expected_and_observed_commit(self):
        self.scenario(head_sha='b' * 40)
        result = self.cli('submit', '--repo', REPO, ok=False)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error'], 'commit_mismatch')
        self.assertEqual(result['commit'], SHA)
        self.assertEqual(result['observed_commit'], 'b' * 40)
        self.assertEqual(result['run_id'], 42)
        self.assertEqual(len(self.calls()), 3)

    def test_dispatch_failure_is_sanitized_and_never_retried(self):
        self.scenario(dispatch_fail=True)
        result = self.cli('submit', '--repo', REPO, ok=False)
        self.assertEqual(result['error'], 'dispatch_failed')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['commit'], SHA)
        self.assertEqual(len(self.calls()), 2)

    def test_status_is_one_query_and_returns_observed_run_without_wait(self):
        self.scenario(request=REQUEST, head_sha='b' * 40)
        result = self.cli('status', '--repo', REPO, '--request', str(uuid.UUID(REQUEST)))
        self.assertEqual(result['request_id'], REQUEST)
        self.assertEqual(result['commit'], 'b' * 40)
        self.assertEqual(result['status'], 'in_progress')
        self.assertEqual(result['run_id'], 42)
        self.assertTrue(result['url'].endswith('/42'))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0][:2], ['run', 'list'])

    def test_status_missing_is_pending_after_exactly_one_query(self):
        self.scenario(request=REQUEST, delay=20)
        result = self.cli('status', '--repo', REPO, '--request', REQUEST)
        self.assertEqual(result, {'request_id': REQUEST, 'status': 'pending_discovery'})
        self.assertEqual(len(self.calls()), 1)

    def test_status_completed_conclusion_is_observed_not_waited_for(self):
        self.scenario(request=REQUEST, status='completed', conclusion='failure')
        result = self.cli('status', '--repo', REPO, '--request', REQUEST)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['conclusion'], 'failure')
        self.assertEqual(len(self.calls()), 1)

    def test_clean_mode_slash_ref_and_unique_requests(self):
        first = self.cli('submit', '--repo', REPO, '--ref', 'feature/kernel', '--mode', 'clean')
        payload = json.loads((self.root / 'payload').read_text())
        self.assertEqual(payload['ref'], 'feature/kernel')
        self.assertEqual(payload['inputs']['mode'], 'clean')
        self.assertEqual(self.calls()[0][1], f'repos/{REPO}/commits/feature%2Fkernel')
        second = self.cli('submit', '--repo', REPO)
        self.assertNotEqual(first['request_id'], second['request_id'])

    def test_validation_rejects_unsafe_inputs_before_calling_gh(self):
        cases = [
            ('submit', '--repo', repo) for repo in
            ('owner', 'owner/repo/extra', '-owner/repo', 'owner/-repo', 'owner/..',
             'owner/re po', 'owner/repo\n', 'owner/repo?token=secret')]
        cases += [('submit', '--repo', REPO, '--ref=' + ref) for ref in
                  ('', '-main', ' main', 'ma\nin', 'main\x7f', 'main\x80', 'main\u202e', 'main\t', '/main',
                   'main..other', 'main@{1}', 'main?token=secret', 'foo//bar')]
        cases += [('status', '--repo', REPO, '--request=' + value) for value in
                  ('', '-bad', 'not-a-uuid', REQUEST + '\n', REQUEST + '/extra')]
        cases += [('submit', '--repo', REPO, '--mode', 'invalid')]
        for args in cases:
            with self.subTest(args=args):
                result = self.cli(*args, ok=False)
                self.assertEqual(result['status'], 'error')
        self.assertEqual(self.calls(), [])

    def test_resolution_failure_and_invalid_sha_never_dispatch(self):
        for scenario, error in [({'resolve_fail': True}, 'resolve_failed'),
                                ({'sha': 'not-a-commit'}, 'invalid_commit')]:
            with self.subTest(scenario=scenario):
                self.scenario(**scenario)
                result = self.cli('submit', '--repo', REPO, ok=False)
                self.assertEqual(result['error'], error)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse((self.root / 'payload').exists())

    def test_discovery_failure_and_malformed_stdout_are_sanitized(self):
        for scenario, error in [({'discovery_fail': True}, 'discovery_failed'),
                                ({'malformed': True}, 'invalid_run_metadata')]:
            with self.subTest(scenario=scenario):
                self.scenario(request=REQUEST, **scenario)
                result = self.cli('status', '--repo', REPO, '--request', REQUEST, ok=False)
                self.assertEqual(result['error'], error)
        self.assertEqual(len(self.calls()), 2)

    def test_slow_discovery_exhausts_budget_without_additional_queries(self):
        self.scenario(slow=True)
        start = time.monotonic()
        result = self.cli('submit', '--repo', REPO)
        self.assertLess(time.monotonic() - start, 7)
        self.assertEqual(result['status'], 'pending_discovery')
        self.assertEqual(result['commit'], SHA)
        self.assertEqual(len(self.calls()), 3)


if __name__ == '__main__':
    unittest.main()
