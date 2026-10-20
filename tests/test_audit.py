"""Structural audit CLI tests; synthetic fixtures, not a phone execution test."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='lab-audit-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src, self.obj = self.root / 'source', self.root / 'objects'
        (self.src / 'drivers/usb/gadget/function').mkdir(parents=True)
        (self.src / 'drivers/usb/gadget/function/u_serial.c').write_text(
            '#define GS_CONSOLE_BUF_SIZE 1048576\n')
        boot = self.obj / 'arch/arm64/boot'
        (boot / 'dts/qcom').mkdir(parents=True)
        self.image = boot / 'Image'
        image = bytearray(512)
        image[:2] = b'MZ'
        image[0x38:0x3c] = b'ARMd'
        struct.pack_into('<I', image, 0x3c, 64)
        image[64:68] = b'PE\0\0'
        struct.pack_into('<HH', image, 68, 0xaa64, 1)
        struct.pack_into('<H', image, 84, 112)
        struct.pack_into('<H', image, 88, 0x20b)
        struct.pack_into('<I', image, 104, 256)
        struct.pack_into('<H', image, 156, 10)
        struct.pack_into('<II', image, 208, 256, 256)
        struct.pack_into('<I', image, 236, 0x20000000)
        self.image.write_bytes(image)
        # A real, tiny AArch64 ELF with the required entry symbol.
        asm = self.root / 'entry.S'
        asm.write_text('.text\n.global __efistub_efi_pe_entry\n__efistub_efi_pe_entry:\n ret\n')
        prefix = os.environ['CROSS_COMPILE']
        subprocess.run([prefix + 'gcc', '-nostdlib', '-Wl,-e,__efistub_efi_pe_entry',
                        '-o', str(self.obj / 'vmlinux'), str(asm)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        config = (ROOT / 'config/ace6.config').read_text()
        (self.obj / '.config').write_text(config)
        self.profile = self.root / 'profile.json'
        self.profile.write_text(json.dumps({'dtb': 'qcom/board.dtb'}))
        self.dtb = boot / 'dts/qcom/board.dtb'
        self.dt = '''/dts-v1/; / {
 model = "OnePlus Ace 6 (synthetic audit fixture)";
 usb: usb { status = "okay"; dr_mode = "peripheral"; maximum-speed = "high-speed"; };
 dispcc: dispcc { status = "disabled"; };
 ufs_mem_hc: ufs { status = "disabled"; };
 ufs_mem_phy: ufsphy { status = "disabled"; };
 pcie0: pcie { status = "disabled"; };
 mdss: mdss { status = "disabled"; };
};'''
        self.compile_dt()

    def compile_dt(self):
        subprocess.run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', str(self.dtb), '-'],
                       input=self.dt.encode(), check=True, stderr=subprocess.PIPE)

    def audit(self, ok):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/audit.py'),
            '--source', str(self.src), '--build', str(self.obj), '--profile', str(self.profile)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)

    def test_valid_structural_pair(self):
        self.audit(True)

    def test_missing_or_corrupt_image_and_dtb(self):
        original = self.image.read_bytes()
        self.image.write_bytes(b'not an arm64 image')
        self.audit(False)
        self.image.write_bytes(original)
        self.dtb.unlink()
        self.audit(False)

    def test_unsafe_config_and_source_fifo_rejected(self):
        path = self.obj / '.config'
        original = path.read_text()
        for old, new in [('# CONFIG_BLOCK is not set', 'CONFIG_BLOCK=y'),
                         ('CONFIG_INITRAMFS_SOURCE=""', 'CONFIG_INITRAMFS_SOURCE="/private/archive"')]:
            path.write_text(original.replace(old, new))
            self.audit(False)
        path.write_text(original)
        (self.src / 'drivers/usb/gadget/function/u_serial.c').write_text(
            '#define GS_CONSOLE_BUF_SIZE 8192\n')
        self.audit(False)

    def test_unexpected_storage_or_usb_role_rejected(self):
        self.dt = self.dt.replace('ufs { status = "disabled"', 'ufs { status = "okay"')
        self.compile_dt()
        self.audit(False)
        self.dt = self.dt.replace('ufs { status = "okay"', 'ufs { status = "disabled"')
        self.dt = self.dt.replace('dr_mode = "peripheral"', 'dr_mode = "host"')
        self.compile_dt()
        self.audit(False)

    def test_bad_pe_entry_rejected(self):
        image = bytearray(self.image.read_bytes())
        struct.pack_into('<I', image, 104, 0x10000)
        self.image.write_bytes(image)
        self.audit(False)


if __name__ == '__main__':
    unittest.main()
