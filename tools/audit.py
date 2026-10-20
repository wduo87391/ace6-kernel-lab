#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fail-closed structural checks; never a hardware/boot/safety certification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


class AuditError(Exception):
    pass


def check(ok, message):
    if not ok:
        raise AuditError(message)


def output(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True,
                            env={**os.environ, 'LC_ALL': 'C'})
    check(result.returncode == 0, 'audit tool failed')
    return result.stdout.strip()


def config_values(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
            values[key[7:]] = value
        else:
            match = re.fullmatch(r'# CONFIG_(\w+) is not set', line)
            if match:
                values[match[1]] = 'n'
    return values


def audit(source, build, profile):
    dtb_name = json.loads(profile.read_text())['dtb']
    check(re.fullmatch(r'qcom/[A-Za-z0-9_-]+\.dtb', dtb_name), 'invalid DTB target')
    config = config_values(build / '.config')
    required = ('ARM64', 'EFI', 'EFI_STUB', 'CMDLINE_FORCE', 'BLK_DEV_INITRD',
                'USB', 'USB_G_SERIAL', 'U_SERIAL_CONSOLE', 'DEBUG_INFO_NONE')
    for name in required:
        check(config.get(name) == 'y', 'required config dropped: ' + name)
    for name in ('MODULES', 'BLOCK', 'NETDEVICES', 'PCI', 'LOCALVERSION_AUTO'):
        check(config.get(name, 'n') == 'n', 'unexpected config enabled: ' + name)
    check(config.get('INITRAMFS_SOURCE') == '""', 'embedded userspace is not allowed in this profile')
    check(config.get('EXTRA_FIRMWARE') == '""', 'embedded firmware is not allowed in this profile')
    cmdline = config.get('CMDLINE', '')
    for token in ('rdinit=/init', 'noinitrd', 'console=ttyGS0', 'clk_ignore_unused',
                  'pd_ignore_unused', 'cpuidle.off=1', 'arm-smmu.disable_bypass=0'):
        check(token in cmdline.strip('"').split(), 'missing boot-path token')
    check(re.search(r'^#define\s+GS_CONSOLE_BUF_SIZE\s+1048576\s*$',
          (source / 'drivers/usb/gadget/function/u_serial.c').read_text(), re.M), 'wrong console FIFO')

    image = (build / 'arch/arm64/boot/Image').read_bytes()
    check(len(image) >= 128 and image[:2] == b'MZ' and image[0x38:0x3c] == b'ARMd',
          'invalid ARM64 EFI Image')
    pe = struct.unpack_from('<I', image, 0x3c)[0]
    check(pe + 24 <= len(image) and image[pe:pe+4] == b'PE\0\0', 'invalid PE header')
    machine, sections = struct.unpack_from('<HH', image, pe+4)
    size = struct.unpack_from('<H', image, pe+20)[0]
    opt = pe+24
    check(size >= 70 and opt+size+sections*40 <= len(image), 'truncated PE sections')
    magic = struct.unpack_from('<H', image, opt)[0]
    entry = struct.unpack_from('<I', image, opt+16)[0]
    subsystem = struct.unpack_from('<H', image, opt+68)[0]
    check((machine, magic, subsystem) == (0xaa64, 0x20b, 10), 'wrong EFI architecture or subsystem')
    executable = False
    for i in range(sections):
        section = opt+size+i*40
        length, rva = struct.unpack_from('<II', image, section+8)
        flags = struct.unpack_from('<I', image, section+36)[0]
        executable |= rva <= entry < rva+length and bool(flags & 0x20000000)
    check(entry > 0 and executable, 'EFI entry is not executable')
    prefix = os.environ['CROSS_COMPILE']
    elf = output([prefix+'readelf', '-h', build / 'vmlinux'])
    check(re.search(r'Machine:\s+AArch64', elf), 'wrong vmlinux architecture')
    symbols = output([prefix+'nm', '--defined-only', build / 'vmlinux'])
    check(re.search(r' [tT] __efistub_efi_pe_entry$', symbols, re.M), 'missing linked EFI entry')

    dtb = build / 'arch/arm64/boot/dts' / dtb_name
    check(dtb.stat().st_size > 0, 'empty DTB')

    def prop(node, name):
        return output(['fdtget', '-t', 's', dtb, node, name])

    def available(node):
        # Check every ancestor, not just the leaf's status.
        parts = node.strip('/').split('/')
        for i in range(1, len(parts)+1):
            path = '/'+'/'.join(parts[:i])
            names = output(['fdtget', '-p', dtb, path]).splitlines()
            if 'status' in names and prop(path, 'status') not in ('okay', 'ok'):
                return False
        return True

    check(prop('/', 'model').startswith('OnePlus Ace 6'), 'unexpected board model')
    usb = prop('/__symbols__', 'usb')
    check(available(usb) and prop(usb, 'dr_mode') == 'peripheral'
          and prop(usb, 'maximum-speed') == 'high-speed', 'USB profile mismatch')
    for name in ('dispcc', 'ufs_mem_hc', 'ufs_mem_phy', 'pcie0', 'mdss'):
        check(not available(prop('/__symbols__', name)), 'unexpected DT consumer enabled: '+name)
    return {'schema': 1, 'structural_checks': 'passed', 'device_validated': False,
            'Image_sha256': hashlib.sha256(image).hexdigest(),
            'dtb_sha256': hashlib.sha256(dtb.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'build', 'profile'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.source, args.build, args.profile), sort_keys=True))
    except (AuditError, OSError, ValueError, KeyError, struct.error) as error:
        # Do not expose local paths or raw subprocess output in CI summaries.
        message = str(error) if isinstance(error, AuditError) else 'invalid or missing build data'
        print('audit: '+message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
