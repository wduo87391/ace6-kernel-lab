#!/usr/bin/env python3
"""Local, audited kernel builds. Never deploys; subprocess logs stay in the workspace."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

RECIPE = 1
OWNER = '.kernel-lab-owner'


class BuildError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BuildError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def no_links(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink path refused')


def regular(path):
    no_links(path)
    require(path.is_file(), 'required regular file missing')


def private_dir(path):
    no_links(path)
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o022, 'unsafe directory ownership or permissions')


def owned_dir(path, identity):
    no_links(path)
    marker = path / OWNER
    if path.exists():
        private_dir(path)
        regular(marker)
        require(marker.read_bytes() == encoded(identity), 'unowned directory refused')
    else:
        path.mkdir(mode=0o700, parents=True)
        marker.write_bytes(encoded(identity))


def run(argv, *, log=None, cwd=None, env=None):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=log or subprocess.PIPE,
                            stderr=log or subprocess.DEVNULL)
    require(result.returncode == 0, 'subprocess failed; details remain in local logs')
    return result.stdout


def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'  # Read identity without refreshing the index on disk.
    return run(['git', '-C', repo, *args], env=env)


def snapshot(repo):
    top = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip())
    require(top.resolve() == repo.resolve(), 'repository root must match public checkout')
    commit = git(repo, 'rev-parse', '--verify', 'HEAD').decode().strip()
    require(re.fullmatch(r'[0-9a-f]{40,64}', commit), 'public commit required')
    status = git(repo, 'status', '--porcelain=v1', '--untracked-files=all')
    paths = git(repo, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    contents = {}
    for name in sorted(set(paths.split(b'\0')) - {b''}):
        path = repo / os.fsdecode(name)
        if path.is_symlink():
            value = digest(os.fsencode(os.readlink(path)))
        elif path.is_file():
            value = file_hash(path)
        else:
            value = 'missing'
        contents[os.fsdecode(name)] = value
    state = digest(encoded(contents) + status + git(repo, 'ls-files', '--stage', '-z'))
    return commit, bool(status), state


def relative(name):
    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.+/-]+', name)
            and not PurePosixPath(name).is_absolute()
            and all(p not in ('.', '..') for p in name.split('/')), 'invalid profile path')
    return name


def inputs(repo):
    regular(repo / 'profile.json')
    profile = json.loads((repo / 'profile.json').read_text())
    require(profile['schema'] == 1 and re.fullmatch(r'[a-z0-9-]+', profile['name']),
            'unsupported profile')
    relative(profile['dtb'])
    require(profile['dtb'].startswith('qcom/') and profile['dtb'].endswith('.dtb'), 'invalid DTB')
    require(isinstance(profile['dts'], list) and isinstance(profile['patches'], list), 'invalid inputs')
    names = ['profile.json', 'flake.lock', 'flake.nix', 'kernel-lab',
             'tools/build.py', 'tools/audit.py', profile['config'],
             *profile['dts'], *profile['patches']]
    hashes = {}
    for name in names:
        relative(name)
        regular(repo / name)
        hashes[name] = file_hash(repo / name)
    require(len({Path(p).name for p in profile['dts']}) == len(profile['dts']), 'duplicate DTS basename')
    lock = json.loads((repo / 'flake.lock').read_text())
    nodes = lock['nodes']
    deps = {}
    for name in ('linux', 'nixpkgs'):
        node = nodes[lock['root']]['inputs'][name]
        rev = nodes[node]['locked']['rev']
        require(isinstance(rev, str) and re.fullmatch(r'[0-9a-f]{40,64}', rev), 'invalid dependency pin')
        deps[name] = rev
    return profile, hashes, deps


def writable_tree(root):
    for directory, dirs, files in os.walk(root):
        for path in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


def prepare(repo, pristine, lane, profile, hashes, log):
    source, marker = lane / 'source', lane / 'prepared.json'
    fingerprint = digest(encoded({key: [(n, hashes[n]) for n in profile[key]]
                                 for key in ('patches', 'dts')}))
    no_links(marker)
    no_links(source)
    if marker.exists() and marker.read_text() == fingerprint and source.is_dir():
        return source
    stage = lane / ('prepare-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        run(['cp', '-a', '--reflink=auto', str(pristine) + '/.', stage], log=log)
        writable_tree(stage)
        for patch in profile['patches']:
            run(['patch', '--batch', '--forward', '--fuzz=0', '-p1', '--input', repo / patch],
                cwd=stage, log=log)
        destination = stage / 'arch/arm64/boot/dts/qcom'
        no_links(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for dts in profile['dts']:
            target = destination / Path(dts).name
            no_links(target)
            shutil.copyfile(repo / dts, target)
        private_dir(source)
        marker.unlink(missing_ok=True)  # Interrupted synchronization must be retried.
        run(['rsync', '-rc', '--links', '--delete', str(stage) + '/', str(source) + '/'], log=log)
        marker.write_text(fingerprint)
    finally:
        shutil.rmtree(stage)
    return source


def build_env(work, prefix, source, objects):
    # Build only in the pinned tool environment, not the caller's make/Kconfig
    # session. Inherited dry-run flags can otherwise relabel stale artifacts;
    # KCONFIG_* output overrides can write outside the owned workspace.
    keep = {'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'CCACHE_DISABLE', 'SSL_CERT_FILE'}
    env = {key: value for key, value in os.environ.items()
           if key in keep or key.startswith('NIX_')}
    maps = ' '.join(f'-{flag}={path}=.' for path in (work, source, objects)
                    for flag in ('ffile-prefix-map', 'fdebug-prefix-map', 'fmacro-prefix-map'))
    env.pop('NIX_LDFLAGS', None)
    env.pop('NIX_CFLAGS_COMPILE', None)
    env.update(ARCH='arm64', CROSS_COMPILE=prefix, LC_ALL='C', KBUILD_BUILD_USER='kernel-lab',
               KBUILD_BUILD_HOST='builder', KBUILD_BUILD_VERSION='1',
               KBUILD_BUILD_TIMESTAMP='Thu Jan  1 00:00:01 UTC 1970', SOURCE_DATE_EPOCH='1',
               KCONFIG_NOTIMESTAMP='1', LOCALVERSION='', KCFLAGS=maps, DTC_FLAGS='-@',
               CCACHE_COMPILERCHECK='content', CCACHE_BASEDIR=str(work),
               CCACHE_NOHASHDIR='true', CCACHE_DIR=str(work / 'ccache'))
    compiler = prefix + 'gcc'
    cache = shutil.which('ccache')
    cc = f'{cache} {compiler}' if cache else compiler
    return env, ['ARCH=arm64', f'CROSS_COMPILE={prefix}', f'CC={cc}',
                 f'KCFLAGS={maps}', 'DTC_FLAGS=-@', 'LOCALVERSION=']


def publish(repo, work, objects, profile, manifest, initial):
    artifacts = work / 'artifacts'
    private_dir(artifacts)
    ident = str(uuid.uuid4())
    stage, final = artifacts / ('.stage-' + ident), artifacts / ident
    stage.mkdir(mode=0o700)
    link = work / ('.latest-' + ident)
    try:
        paths = {'Image': objects / 'arch/arm64/boot/Image',
                 Path(profile['dtb']).name: objects / 'arch/arm64/boot/dts' / profile['dtb'],
                 'config.solved': objects / '.config'}
        for name, path in paths.items():
            regular(path)
            require(path.stat().st_size > 0, 'empty artifact refused')
            shutil.copyfile(path, stage / name)
        manifest['artifacts'] = {name: file_hash(stage / name) for name in paths}
        (stage / 'manifest.json').write_bytes(encoded(manifest))
        sums = {**manifest['artifacts'], 'manifest.json': file_hash(stage / 'manifest.json')}
        (stage / 'SHA256SUMS').write_text(''.join(f'{sums[n]}  {n}\n' for n in sorted(sums)))
        require(all(file_hash(stage / n) == h for n, h in sums.items()), 'artifact verification failed')
        require(snapshot(repo) == initial, 'repository changed during build; publication refused')
        # Every declared input is checked too, including ignored profile inputs.
        require(inputs(repo)[1] == manifest['inputs'], 'inputs changed during build; publication refused')
        latest = work / 'latest'
        require(not latest.exists() or latest.is_symlink(), 'unsafe latest target')
        for path in stage.iterdir():
            path.chmod(0o444)
        stage.chmod(0o555)
        stage.rename(final)
        link.symlink_to('artifacts/' + ident)
        os.replace(link, latest)
        return ident, sums
    finally:
        link.unlink(missing_ok=True)
        if stage.exists():
            stage.chmod(0o700)
            shutil.rmtree(stage)


def build(repo, *, workspace, clean=False, allow_dirty=False, jobs=1):
    started = time.monotonic()
    initial = snapshot(repo)
    require(allow_dirty or not initial[1], 'dirty repository refused; use --allow-dirty')
    profile, hashes, deps = inputs(repo)
    pristine = Path(os.environ['KERNEL_SOURCE'])
    prefix = os.environ['CROSS_COMPILE']
    toolchain = os.environ['KERNEL_TOOLCHAIN_ID']
    require(pristine.is_absolute() and pristine.is_dir(), 'invalid KERNEL_SOURCE')
    require(os.path.isabs(prefix) and not any(c.isspace() for c in prefix)
            and os.access(prefix + 'gcc', os.X_OK), 'invalid CROSS_COMPILE')
    require(bool(toolchain), 'missing toolchain identity')
    toolchain_id = 'sha256:' + digest(toolchain.encode())
    work = Path(os.path.abspath(workspace.expanduser()))
    require(not any(c.isspace() for c in str(work)), 'workspace must be whitespace-free')
    require(work != repo and repo not in work.parents and work not in repo.parents,
            'workspace must be separate from repository')
    require(work != pristine and pristine not in work.parents and work not in pristine.parents,
            'workspace must be separate from pristine source')
    owned_dir(work, {'schema': RECIPE, 'uid': os.getuid()})
    lockpath = work / 'build.lock'
    no_links(lockpath)
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1, 'unsafe build lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildError('another build holds the workspace lock') from None
        lane_id = digest(encoded({'schema': RECIPE, 'dependencies': deps, 'toolchain': toolchain_id,
                                  'config': hashes[profile['config']]}))
        private_dir(work / 'lanes')
        lane = work / 'lanes' / lane_id
        owned_dir(lane, {'schema': RECIPE, 'lane': lane_id})
        objects = lane / 'objects'
        no_links(objects)
        no_links(lane / 'prepared.json')
        if clean:
            if objects.exists():
                private_dir(objects)
                shutil.rmtree(objects)
            (lane / 'prepared.json').unlink(missing_ok=True)
        private_dir(objects)
        private_dir(work / 'logs')
        with (work / 'logs' / (uuid.uuid4().hex + '.log')).open('wb') as log:
            source = prepare(repo, pristine, lane, profile, hashes, log)
            config = objects / '.config'
            no_links(config)
            # Always solve from declared intent, not a prior olddefconfig result:
            # changed Kconfig dependencies may have dropped a requested option.
            seed = objects / '.config.next'
            no_links(seed)
            no_links(objects / '.config.next.old')
            shutil.copyfile(repo / profile['config'], seed)
            env, variables = build_env(work, prefix, source, objects)
            command = ['make', '-C', source, f'O={objects}', f'-j{jobs}', *variables]
            run([*command, f'KCONFIG_CONFIG={seed}', 'olddefconfig'], log=log, env=env)
            if not config.exists() or config.read_bytes() != seed.read_bytes():
                os.replace(seed, config)
            else:
                seed.unlink()
            run([*command, 'Image', profile['dtb']], log=log, env=env)
            run([sys.executable, repo / 'tools/audit.py', '--source', source,
                 '--build', objects, '--profile', repo / 'profile.json'], log=log, env=env)
            manifest = {'schema': RECIPE, 'profile': profile['name'], 'commit': initial[0],
                        'dirty': initial[1], 'inputs': hashes, 'dependencies': deps,
                        'toolchain': toolchain_id, 'mode': 'clean' if clean else 'incremental',
                        'structural_audit': 'passed', 'device_validated': False}
            ident, sums = publish(repo, work, objects, profile, manifest, initial)
    print(f'artifacts/{ident} ({time.monotonic() - started:.1f}s)')
    for name, value in sorted(sums.items()):
        print(f'{value}  {name}')


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('build')
    command.add_argument('--clean', action='store_true')
    command.add_argument('--allow-dirty', action='store_true')
    command.add_argument('--workspace', type=Path, default=Path('~/.cache/ace6-kernel-lab'))
    command.add_argument('--jobs', type=positive, default=os.cpu_count() or 1)
    args = vars(parser.parse_args())
    args.pop('command')
    try:
        build(Path(__file__).resolve().parent.parent, **args)
    except BuildError as error:
        print(f'build: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('build: invalid input or local operation failed; no publication', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
