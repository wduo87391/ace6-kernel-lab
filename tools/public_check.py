#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Release allowlist and obvious-identifier gate, not a proof of secret absence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

FILES = {
    '.gitignore', '.github/workflows/kernel.yml', 'LICENSE', 'README.md',
    'flake.nix', 'flake.lock', 'kernel-lab', 'profile.json', 'config/ace6.config',
    'dts/ace6-ktm-ti.dts', 'dts/ace6-ktm-ti-usb-diag.dts', 'dts/ace6-ktm-ti-usb-diag-fb.dts',
    'patches/0005-efi-stub-entry-markers-candidate.patch',
    'patches/0006-efi-stub-post-ebs-markers-candidate.patch',
    'patches/0009-usb-serial-console-1m.patch',
    'tools/build.py', 'tools/audit.py', 'tools/public_check.py', 'tools/ci.py',
    'tests/test_build.py', 'tests/test_audit.py', 'tests/test_ci.py', 'tests/test_public_check.py',
}
ARTIFACTS = {'Image', 'ace6-ktm-ti-usb-diag-fb.dtb', 'config.solved', 'manifest.json', 'SHA256SUMS'}
# Written as fragments to avoid matching this scanner's own source literals.
PATTERNS = [
    rb'/' + rb'(?:home|Users)/[^\s/]+/',
    rb'-----BEGIN ' + rb'(?:OPENSSH|RSA|EC|DSA)? ?PRIVATE KEY-----',
    rb'(?:ssh-ed25519|ssh-rsa)' + rb'\s+[A-Za-z0-9+/]{40,}',
    rb'\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b',
    rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b',
    rb'\b(?=[A-Z0-9]{15,20}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]+\b',
]


# Linux's public printf/hex conversion alphabet is not a unit serial. Keep
# this exact exception narrow; arbitrary hex-only serials remain suspicious.
SAFE_LITERALS = {b'0123456789ABCDEF'}


def scan(name, data):
    for pattern in PATTERNS:
        for match in re.finditer(pattern, data):
            if match.group() not in SAFE_LITERALS:
                raise ValueError('potential identifier in '+name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                                 '--exclude-standard', '-z'], capture_output=True, check=True)
        names = {x.decode() for x in result.stdout.split(b'\0') if x}
        if names != FILES:
            raise ValueError('public file allowlist mismatch')
        for name in sorted(names):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('non-regular public file: '+name)
            scan(name, path.read_bytes())
        if args.artifacts:
            status = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1',
                                              '--untracked-files=all'])
            if status:
                raise ValueError('release requires a clean checkout')
            # Also compare content directly: assume-unchanged flags can hide a
            # modified working file from git status.
            for name in sorted(FILES):
                committed = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:'+name])
                if committed != (root / name).read_bytes():
                    raise ValueError('working source differs from public commit')
            folder = args.artifacts.resolve(strict=True)
            if {p.name for p in folder.iterdir()} != ARTIFACTS:
                raise ValueError('artifact allowlist mismatch')
            sums = {}
            for line in (folder / 'SHA256SUMS').read_text().splitlines():
                sha, name = line.split('  ')
                if name not in ARTIFACTS - {'SHA256SUMS'} or name in sums:
                    raise ValueError('unexpected checksum entry')
                sums[name] = sha
            if set(sums) != ARTIFACTS - {'SHA256SUMS'}:
                raise ValueError('incomplete checksum list')
            for name in sorted(ARTIFACTS):
                path = folder / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError('non-regular artifact')
                data = path.read_bytes()
                # Text scanner on printable strings avoids random binary false positives.
                for text in re.findall(rb'[\x20-\x7e]{8,}', data):
                    scan(name, text)
                if name in sums and hashlib.sha256(data).hexdigest() != sums[name]:
                    raise ValueError('artifact checksum mismatch')
            manifest = json.loads((folder / 'manifest.json').read_text())
            sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
            if (manifest['commit'] != sha or manifest['dirty'] is not False
                    or manifest['device_validated'] is not False):
                raise ValueError('artifact is not a clean compile-only candidate for HEAD')
            profile = json.loads((root / 'profile.json').read_text())
            expected = {'profile.json', 'flake.lock', 'flake.nix', 'kernel-lab',
                        'tools/build.py', 'tools/audit.py', profile['config'],
                        *profile['dts'], *profile['patches']}
            if not expected <= FILES or set(manifest['inputs']) != expected:
                raise ValueError('manifest input set mismatch')
            for name in expected:
                if hashlib.sha256((root / name).read_bytes()).hexdigest() != manifest['inputs'][name]:
                    raise ValueError('manifest input hash mismatch')
            expected_artifacts = ARTIFACTS - {'manifest.json', 'SHA256SUMS'}
            if set(manifest['artifacts']) != expected_artifacts:
                raise ValueError('manifest artifact set mismatch')
            if any(manifest['artifacts'][name] != sums[name] for name in expected_artifacts):
                raise ValueError('manifest artifact hash mismatch')
        print('public allowlist/identifier gate passed (manual review still required)')
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ValueError) else 'invalid release data'
        print('public-check: '+message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
