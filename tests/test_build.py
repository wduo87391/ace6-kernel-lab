"""CLI integration tests: tiny real make builds, NOT Linux/device validation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


DRIVER = Path(__file__).resolve().parents[1] / "tools/build.py"
MAKEFILE = """.PHONY: olddefconfig Image qcom/board.dtb
olddefconfig:
\t@printf 'olddefconfig\\n' >> $(O)/calls
\t@python3 solve.py $(if $(KCONFIG_CONFIG),$(KCONFIG_CONFIG),$(O)/.config)
Image: $(O)/arch/arm64/boot/Image
qcom/board.dtb: $(O)/arch/arm64/boot/dts/qcom/board.dtb
$(O)/arch/arm64/boot/Image: base.txt arch/arm64/boot/dts/qcom/board.dts $(O)/.config
\t@python3 generate.py $(O) Image
$(O)/arch/arm64/boot/dts/qcom/board.dtb: base.txt arch/arm64/boot/dts/qcom/board.dts $(O)/.config
\t@python3 generate.py $(O) qcom/board.dtb
"""
GENERATOR = """import os, pathlib, shlex, subprocess, sys
out, target = pathlib.Path(sys.argv[1]), sys.argv[2]
base = pathlib.Path('base.txt').read_bytes()
if b'FAIL-DTB' in base:
    if target.endswith('.dtb'): sys.exit(7)
elif b'FAIL' in base: sys.exit(7)
assert os.environ['KBUILD_BUILD_USER'] == 'kernel-lab'
assert os.environ['KBUILD_BUILD_HOST'] == 'builder'
assert '-ffile-prefix-map=' in os.environ['KCFLAGS']
assert os.environ['CCACHE_COMPILERCHECK'] == 'content'
assert os.environ['DTC_FLAGS'] == '-@'
subprocess.run(shlex.split(os.environ['CC']), check=True)
data = base + pathlib.Path('arch/arm64/boot/dts/qcom/board.dts').read_bytes() + (out / '.config').read_bytes()
path = out / 'arch/arm64/boot' / ('Image' if target == 'Image' else 'dts/' + target)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(data)
with (out / 'calls').open('a') as f: f.write(target + '\\n')
"""
AUDIT = """import argparse, pathlib
p = argparse.ArgumentParser()
for key in ('source', 'build', 'profile'): p.add_argument('--' + key, required=True)
a = p.parse_args()
root, obj = pathlib.Path(a.profile).parent, pathlib.Path(a.build)
assert (obj / 'arch/arm64/boot/Image').read_bytes() == (obj / 'arch/arm64/boot/dts/qcom/board.dtb').read_bytes()
assert not (root / 'audit-fail').exists()
if (root / 'mutate').exists():
    with (root / 'config/test.config').open('a') as f: f.write('# changed during audit\\n')
"""
PATCH = """--- a/base.txt
+++ b/base.txt
@@ -1 +1 @@
-pristine
+patched
"""


class CompilerPathTests(unittest.TestCase):
    def test_ccache_and_uncached_compiler_embed_identical_file_paths(self):
        prefix, cache = os.environ.get('CROSS_COMPILE'), shutil.which('ccache')
        if not prefix or not cache:
            self.skipTest('requires pinned cross compiler and ccache from nix develop')
        build_env = runpy.run_path(str(DRIVER))['build_env']
        with tempfile.TemporaryDirectory(prefix='kernel-path-test-') as directory:
            work = Path(directory)
            source, objects = work / 'lanes/test/source', work / 'lanes/test/objects'
            source.mkdir(parents=True)
            objects.mkdir()
            probes = {'c': 'const char filename[] = __FILE__;\n',
                      'S': '.section .rodata\n.asciz __FILE__\n'}
            for suffix, text in probes.items():
                with self.subTest(language=suffix):
                    (source / ('probe.'+suffix)).write_text(text)
                    env, _ = build_env(work, prefix, source, objects)
                    flags = env['KCFLAGS' if suffix == 'c' else 'KAFLAGS']
                    command = [cache, prefix+'gcc', *shlex.split(flags), '-c',
                               str(source / ('probe.'+suffix)), '-o', str(objects / 'probe.o')]
                    env['CCACHE_DISABLE'] = '1'
                    subprocess.run(command, cwd=objects, env=env, check=True, capture_output=True)
                    uncached = (objects / 'probe.o').read_bytes()
                    env.pop('CCACHE_DISABLE')
                    subprocess.run(command, cwd=objects, env=env, check=True, capture_output=True)
                    cached = (objects / 'probe.o').read_bytes()
                    self.assertEqual(cached, uncached, 'ccache must not change embedded __FILE__ paths')


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='kernel-lab-test-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo, self.src, self.work = [self.base / p for p in ('repo', 'linux', 'work')]
        self.repo.mkdir()
        self.src.mkdir()
        for name, data in {'Makefile': MAKEFILE, 'generate.py': GENERATOR,
                           'base.txt': 'pristine\n', 'unchanged': 'stable\n'}.items():
            (self.src / name).write_text(data)
        (self.src / 'solve.py').write_text("""import pathlib, sys
p = pathlib.Path(sys.argv[1])
text = p.read_text()
if 'disable-feature' in pathlib.Path('base.txt').read_text():
    text = text.replace('CONFIG_TEST=y', '# CONFIG_TEST is not set')
if '# solved' not in text: text += '# solved\\n'
p.write_text(text)
""")
        (self.src / 'linked').symlink_to('unchanged')
        (self.src / 'base.txt').chmod(0o444)
        self.src.chmod(0o555)
        self.put('tools/build.py', DRIVER.read_text())
        self.put('tools/audit.py', AUDIT)
        self.put('config/test.config', 'CONFIG_TEST=y\n# CONFIG_DEBUG_INFO is not set\n')
        self.put('dts/board.dts', 'board-v1\n')
        self.put('patches/test.patch', PATCH)
        self.profile = {'schema': 1, 'name': 'ace6-usb-log', 'config': 'config/test.config',
                        'dtb': 'qcom/board.dtb', 'dts': ['dts/board.dts'],
                        'patches': ['patches/test.patch']}
        self.put('profile.json', json.dumps(self.profile))
        lock = {'root': 'root', 'nodes': {'root': {'inputs': {'linux': 'linux', 'nixpkgs': 'nixpkgs'}},
                'linux': {'locked': {'rev': '1' * 40}}, 'nixpkgs': {'locked': {'rev': '2' * 40}}}}
        self.put('flake.lock', json.dumps(lock))
        self.put('flake.nix', '# fixture tool environment\n')
        self.put('kernel-lab', '# fixture launcher\n')
        compiler = self.base / 'cross-gcc'
        compiler.write_text('#!/bin/sh\nexit 0\n')
        compiler.chmod(0o755)
        self.env = dict(os.environ, KERNEL_SOURCE=str(self.src), CROSS_COMPILE=str(self.base / 'cross-'),
                        KERNEL_TOOLCHAIN_ID='/nix/store/public-toolchain')
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Public Fixture')
        self.commit()

    def put(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'public fixture')
        return self.git('rev-parse', 'HEAD')

    def cli(self, *args, ok=True, workspace=None):
        result = subprocess.run([sys.executable, str(self.repo / 'tools/build.py'), 'build',
                                 '--workspace', str(workspace or self.work), '--jobs', '2', *args],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        self.assertNotIn(str(self.base), result.stdout + result.stderr)
        return result

    def latest(self):
        return (self.work / 'latest').resolve()

    def lane(self):
        return next((self.work / 'lanes').iterdir())

    def test_publication_identity_hashes_and_incremental_reuse(self):
        self.cli()
        first = self.latest()
        manifest = json.loads((first / 'manifest.json').read_text())
        self.assertEqual(manifest['commit'], self.git('rev-parse', 'HEAD'))
        self.assertFalse(manifest['dirty'])
        self.assertFalse(manifest['device_validated'])
        self.assertEqual(manifest['mode'], 'incremental')
        self.assertEqual(manifest['dependencies'], {'linux': '1' * 40, 'nixpkgs': '2' * 40})
        self.assertNotIn('/nix/store', (first / 'manifest.json').read_text())
        for line in (first / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256((first / name).read_bytes()).hexdigest())
        self.assertEqual({p.name for p in first.iterdir()},
                         {'Image', 'board.dtb', 'config.solved', 'manifest.json', 'SHA256SUMS'})
        obj, source = self.lane() / 'objects', self.lane() / 'source'
        image = obj / 'arch/arm64/boot/Image'
        self.assertTrue((source / 'linked').is_symlink())
        stamps = [p.stat().st_mtime_ns for p in (image, source / 'unchanged')]
        (source / 'no-recopy-sentinel').write_text('prepared source reused')
        self.cli()
        self.assertTrue((source / 'no-recopy-sentinel').exists())
        self.assertNotEqual(first, self.latest())
        self.assertEqual(stamps, [p.stat().st_mtime_ns for p in (image, source / 'unchanged')])
        self.assertEqual((obj / 'calls').read_text().count('Image\n'), 1)

    def test_patch_and_dts_updates_and_removals_share_lane(self):
        self.cli()
        lane = self.lane()
        stamp = (lane / 'source/unchanged').stat().st_mtime_ns
        self.put('patches/test.patch', PATCH.replace('+patched', '+new-patch'))
        self.put('dts/board.dts', 'board-v2\n')
        self.put('dts/extra.dts', 'extra\n')
        self.profile['dts'].append('dts/extra.dts')
        self.put('profile.json', json.dumps(self.profile))
        self.commit()
        self.cli()
        self.assertEqual(self.lane(), lane)
        self.assertEqual((self.src / 'base.txt').read_text(), 'pristine\n')
        self.assertIn(b'new-patch\nboard-v2', (self.latest() / 'Image').read_bytes())
        self.assertEqual((lane / 'source/unchanged').stat().st_mtime_ns, stamp)
        self.profile['patches'] = []
        self.profile['dts'].remove('dts/extra.dts')
        self.put('profile.json', json.dumps(self.profile))
        self.commit()
        self.cli()
        self.assertTrue((self.latest() / 'Image').read_bytes().startswith(b'pristine'))
        self.assertFalse((lane / 'source/arch/arm64/boot/dts/qcom/extra.dts').exists())
        self.assertEqual(self.lane(), lane)

    def test_config_lock_and_toolchain_invalidate_lane(self):
        self.cli()
        for change in ('config', 'linux', 'nixpkgs', 'toolchain'):
            with self.subTest(change=change):
                before = len(list((self.work / 'lanes').iterdir()))
                if change == 'config':
                    self.put('config/test.config', 'CONFIG_OTHER=y\n')
                elif change == 'toolchain':
                    self.env['KERNEL_TOOLCHAIN_ID'] = '/nix/store/different-toolchain'
                else:
                    lock = json.loads((self.repo / 'flake.lock').read_text())
                    lock['nodes'][change]['locked']['rev'] = '3' * 40
                    self.put('flake.lock', json.dumps(lock))
                if change != 'toolchain': self.commit()
                self.cli()
                self.assertEqual(len(list((self.work / 'lanes').iterdir())), before + 1)

    def test_config_dependency_change_and_revert_match_clean(self):
        # A dependency change may drop a seeded option. Revert must restore it.
        self.cli()
        self.put('patches/test.patch', PATCH.replace('+patched', '+disable-feature'))
        self.commit()
        self.cli()
        self.assertIn(b'# CONFIG_TEST is not set', (self.latest() / 'config.solved').read_bytes())
        self.put('patches/test.patch', PATCH)
        self.commit()
        self.cli()
        incremental = (self.latest() / 'Image').read_bytes()
        self.assertIn(b'CONFIG_TEST=y', incremental)
        self.cli('--clean')
        self.assertEqual(incremental, (self.latest() / 'Image').read_bytes())

    def test_dirty_refused_and_opt_in_records_commit(self):
        commit = self.git('rev-parse', 'HEAD')
        self.put('dts/board.dts', 'dirty-board\n')
        self.cli(ok=False)
        self.assertFalse((self.work / 'latest').exists())
        self.cli('--allow-dirty')
        manifest = json.loads((self.latest() / 'manifest.json').read_text())
        self.assertEqual(manifest['commit'], commit)
        self.assertTrue(manifest['dirty'])
        self.assertEqual(manifest['inputs']['dts/board.dts'],
                         hashlib.sha256(b'dirty-board\n').hexdigest())

    def test_failures_preserve_previous_pair(self):
        self.cli()
        previous = self.latest()
        for failure in ('audit', 'make', 'dtb', 'patch', 'mutation'):
            with self.subTest(failure=failure):
                self.put('tools/audit.py', AUDIT)
                self.put('patches/test.patch', PATCH)
                for name in ('audit-fail', 'mutate'):
                    (self.repo / name).unlink(missing_ok=True)
                if failure == 'audit': self.put('audit-fail', '')
                if failure == 'make': self.put('patches/test.patch', PATCH.replace('+patched', '+FAIL'))
                if failure == 'dtb': self.put('patches/test.patch', PATCH.replace('+patched', '+FAIL-DTB'))
                if failure == 'patch': self.put('patches/test.patch', PATCH.replace('-pristine', '-missing'))
                if failure == 'mutation': self.put('mutate', '')
                self.commit()
                self.cli(ok=False)
                if failure == 'dtb':
                    boot = self.lane() / 'objects/arch/arm64/boot'
                    self.assertNotEqual((boot / 'Image').read_bytes(), (boot / 'dts/qcom/board.dtb').read_bytes())
                self.assertEqual(self.latest(), previous)
                self.assertEqual((previous / 'Image').read_bytes(), (previous / 'board.dtb').read_bytes())

    def test_clean_only_resets_current_objects(self):
        self.cli()
        lane = self.lane()
        (lane / 'objects/sentinel').write_text('stale')
        unrelated = self.work / 'lanes/unrelated'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('untouched')
        first = self.latest()
        self.cli('--clean')
        self.assertFalse((lane / 'objects/sentinel').exists())
        self.assertEqual((unrelated / 'keep').read_text(), 'untouched')
        self.assertTrue(first.exists())
        self.assertEqual((first / 'Image').read_bytes(), (self.latest() / 'Image').read_bytes())
        self.assertEqual(json.loads((self.latest() / 'manifest.json').read_text())['mode'], 'clean')
        self.assertEqual((lane / 'objects/calls').read_text().count('Image\n'), 1)

    def test_unsafe_workspaces_and_cleanup_symlink_refused(self):
        unknown = self.base / 'unknown'
        unknown.mkdir()
        (unknown / 'keep').write_text('untouched')
        self.cli(workspace=unknown, ok=False)
        self.assertEqual((unknown / 'keep').read_text(), 'untouched')
        alias = self.base / 'alias'
        alias.symlink_to(unknown, target_is_directory=True)
        self.cli(workspace=alias, ok=False)
        self.cli(workspace=self.base / 'has space', ok=False)
        self.cli()
        self.work.chmod(0o777)
        self.cli('--clean', ok=False)
        self.work.chmod(0o700)
        objects = self.lane() / 'objects'
        shutil.rmtree(objects)
        objects.symlink_to(unknown, target_is_directory=True)
        self.cli('--clean', ok=False)
        self.assertEqual((unknown / 'keep').read_text(), 'untouched')

    def test_inherited_make_and_kconfig_controls_cannot_skip_rebuild(self):
        self.cli()
        self.put('dts/board.dts', 'must-rebuild\n')
        self.commit()
        self.env.update(GNUMAKEFLAGS='--just-print', MAKEFILES='/nonexistent/makefile',
                        KCONFIG_AUTOHEADER=str(self.base / 'outside-header'))
        self.cli()
        self.assertIn(b'must-rebuild', (self.latest() / 'Image').read_bytes())
        self.assertFalse((self.base / 'outside-header').exists())

    def test_optional_ccache_wraps_absolute_compiler(self):
        tools = self.base / 'bin'
        tools.mkdir()
        cache = tools / 'ccache'
        used = self.base / 'ccache-used'
        cache.write_text(f'#!/bin/sh\nprintf "cache\\n" >> "{used}"\nexec "$@"\n')
        cache.chmod(0o755)
        self.env['PATH'] = str(tools) + os.pathsep + self.env['PATH']
        self.cli()
        self.assertEqual(used.read_text(), 'cache\ncache\n')

    def test_commit_required_and_enclosing_repository_not_used(self):
        # An enclosing checkout must never supply the public repository identity.
        (self.repo / '.git').rename(self.base / '.git')
        with (self.base / '.git/info/exclude').open('a') as exclude:
            exclude.write('/work/\n/linux/\n/cross-gcc\n')
        result = self.cli('--allow-dirty', ok=False)
        self.assertIn('repository root', result.stderr)
        shutil.rmtree(self.base / '.git')
        self.git('init', '-q')
        self.cli('--allow-dirty', ok=False)

    def test_concurrent_build_refused(self):
        self.cli()
        with (self.work / 'build.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cli(ok=False)


if __name__ == '__main__':
    unittest.main()
