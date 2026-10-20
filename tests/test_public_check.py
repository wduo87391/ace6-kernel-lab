"""Release gate CLI tests, with synthetic public artifacts and isolated Git history."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='public-gate-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.git('init', '-q')
        self.git('config', 'user.name', 'Public Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'public fixture')
        self.artifacts = self.base / 'artifacts'
        self.artifacts.mkdir()
        self.dtb = 'ace6-ktm-ti-usb-diag-fb.dtb'
        self.profile = json.loads((self.repo / 'profile.json').read_text())
        self.inputs = ['profile.json', 'flake.lock', 'flake.nix', 'kernel-lab', 'tools/build.py',
                       'tools/audit.py', self.profile['config'], *self.profile['dts'], *self.profile['patches']]
        self.manifest = {'commit': self.git('rev-parse', 'HEAD'), 'dirty': False,
                         'device_validated': False,
                         'inputs': {name: self.sha((self.repo / name).read_bytes()) for name in self.inputs}}
        for name in ('Image', self.dtb, 'config.solved'):
            (self.artifacts / name).write_text('synthetic public artifact\n')
        self.manifest['artifacts'] = {name: self.sha((self.artifacts / name).read_bytes())
                                      for name in ('Image', self.dtb, 'config.solved')}
        self.save()

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def save(self):
        (self.artifacts / 'manifest.json').write_text(json.dumps(self.manifest))
        names = ('Image', self.dtb, 'config.solved', 'manifest.json')
        (self.artifacts / 'SHA256SUMS').write_text(''.join(
            self.sha((self.artifacts / name).read_bytes())+'  '+name+'\n' for name in names))

    def check(self, ok):
        result = subprocess.run([sys.executable, str(self.repo / 'tools/public_check.py'),
            '--artifacts', str(self.artifacts)], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        self.assertNotIn(str(self.base), result.stdout + result.stderr)

    def test_clean_public_fixture_accepted(self):
        self.check(True)

    def test_dirty_source_cannot_approve_old_committed_artifacts(self):
        with (self.repo / 'flake.nix').open('a') as f:
            f.write('# uncommitted recipe change\n')
        self.check(False)

    def test_manifest_input_and_artifact_hashes_must_match(self):
        for key in ('inputs', 'artifacts'):
            with self.subTest(key=key):
                name = next(iter(self.manifest[key]))
                original = self.manifest[key][name]
                self.manifest[key][name] = '0' * 64
                self.save()  # Even a consistent external checksum must not hide this mismatch.
                self.check(False)
                self.manifest[key][name] = original

    def test_incomplete_input_manifest_rejected(self):
        del self.manifest['inputs']['kernel-lab']
        self.save()
        self.check(False)


if __name__ == '__main__':
    unittest.main()
