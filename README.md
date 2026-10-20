# ace6-kernel-lab

Shared, distribution-independent **kernel development** for OnePlus Ace 6
(PLQ110 / SM8750). Build drivers without rebuilding a complete NixOS system.
The eventual user-space targets are postmarketOS for driver development and
NixOS for system integration; neither is included in this first slice.

**Compile-only. NOT a flashable image or a bootable phone system. Do not flash.**
The public baseline has no embedded `/init` or root filesystem, retains
`noinitrd` and the forced diagnostic command line, and cannot reproduce an
older successful user-space boot. A later, separately tested initramfs/loader
integration is required. No deployment commands are provided.

## Inputs and scope

- Linux [af32da41b0327b9c6a37856ba82b6760d6c8d10e](https://github.com/torvalds/linux/commit/af32da41b0327b9c6a37856ba82b6760d6c8d10e).
- nixpkgs [7fd4004d0511991fff3a0524856a9be09e28c5f0](https://github.com/NixOS/nixpkgs/commit/7fd4004d0511991fff3a0524856a9be09e28c5f0), pinned AArch64 musl cross compiler.
- One USB-log profile: EFI markers, firmware framebuffer workarounds,
  USB peripheral/high-speed partial graph, 1 MiB serial-console FIFO.
- No UFS/block access, PCI/Wi-Fi, modules, external firmware or device logs in
  this profile. This is not a zero-write guarantee for boot firmware.
- RAM/reservations still inherit the upstream reference board. SoC source
  support, compilation, board correctness and actual device operation are
  different evidence levels. This repository proves no new hardware support.

The two EFI patches and the USB-console change are development instrumentation,
not upstream acceptance or electrical validation. Clock/power retention and
SMMU bypass are diagnostic workarounds, not final power/security policies.

## Local use (x86_64 Linux, Nix with flakes)

```sh
./kernel-lab test
./kernel-lab build --jobs 6
./kernel-lab build --allow-dirty --jobs 6  # local, explicitly marked development
CCACHE_DISABLE=1 ./kernel-lab build --clean --jobs 6
```

Nix supplies pinned sources and tools; it does **not** rebuild the kernel in a
fresh derivation for each edit. The first invocation may fetch/build substantial
dependencies. No NixOS host configuration, privileged operation, daemon sandbox
exception, global package installation or phone connection is needed.

Persistent state lives in `~/.cache/ace6-kernel-lab`, separate from the checkout.
Override with `build --workspace /a/whitespace-free/user-owned/path`. The checkout
may contain spaces, but Kbuild's source/object paths must not. Unknown preexisting
workspaces, unsafe ownership, symlink paths and concurrent builds are rejected.
Do not manually edit prepared source: edit the public patch/DTS/config inputs.

- Linux/nixpkgs/compiler/config changes select a new cache lane.
- Ordered patch/DTS updates or removals are replayed on pristine source and
  checksum-synchronized; unchanged source mtimes and Kbuild objects survive.
- `--clean` resets only the selected lane's objects; also set `CCACHE_DISABLE=1`
  for a genuine compiler-cache-independent build. Old artifacts/other lanes stay.
- A workspace lock prevents two writers. Dirty input is refused by default;
  input mutation during a build prevents publication.

## Outputs and privacy

After compilation, solved-config, ARM64/EFI entry, linked-symbol and USB/disabled
consumer DT checks must pass. Only then does `latest` atomically select an
immutable artifact directory containing:

- `Image` and **its matching** `ace6-ktm-ti-usb-diag-fb.dtb`;
- `config.solved`, `manifest.json` and `SHA256SUMS`.

A failed build/audit never selects a half-new pair. Manifest records the public
commit, dirty flag, input hashes, dependency revisions, toolchain hash, build
mode, structural-audit status and `device_validated: false`—not personal paths,
hostnames, phone IDs, root images or original private payload hashes. Build
user/host/time are neutral constants and source paths are prefix-mapped.
Local raw compiler logs stay in the workspace and are **not uploaded**.

`tools/public_check.py` enforces a filename allowlist and screens common identity
and credential formats; `--artifacts PATH` also checks output hashes/identity.
This is defense in depth, not proof that arbitrary future code or binary output
contains no secrets. Review additions manually. Do not commit keys (including
personal SSH public keys), serials, IMEI/UUID/MAC, dumps, stock partitions,
runtime memory captures, original Git history or host/device logs. Board model,
register addresses, tuning and firmware framebuffer geometry are functional
hardware descriptions, not per-unit identifiers.

## Asynchronous GitHub Actions

Push a **small, fixed commit** on each driver branch. Actions tests and builds
that commit independently; continue other unblocked driver work while it runs.
Do not wait in a `gh run watch` loop. Query when reaching a dependency boundary:

```sh
./kernel-lab ci submit --repo OWNER/ace6-kernel-lab --ref main
./kernel-lab ci submit --repo OWNER/ace6-kernel-lab --ref main --mode clean
./kernel-lab ci status --repo OWNER/ace6-kernel-lab --request REQUEST_ID
gh run view RUN_ID --repo OWNER/ace6-kernel-lab --json status,conclusion,url,headSha
```

Submission resolves the branch SHA, dispatches a unique request ID and discovers
only run metadata (at most three queries within five seconds, never compilation
completion). It returns the commit and run ID/URL when visible, otherwise explicit
`pending_discovery` plus the request ID for a later one-shot status query. The
workflow refuses a moved ref that no longer matches the requested SHA. Use a
branch with its intended commit pushed; submission does not push local files.
The CI helper needs Python 3 and an already authenticated `gh` on the submitting
machine; build/test do not require GitHub credentials.

GitHub-hosted disposable runners only; read-only token, no secrets, pinned
actions, no `pull_request_target`, checkout credentials not persisted. PRs can
read public development caches; only successful main/non-PR runs save them.
Caches are mutable development acceleration, **not release trust evidence**;
use clean runs for important candidates. Only five explicit audited artifacts
are uploaded (7-day retention), not the workspace, logs or Nix store. Nix binary
cache publication is not configured. Runner capacity/queue/cache transfer may
make individual remote builds slower than local ones; parallelism is the goal.

## Driver and user-space integration

1. Keep a fixed known baseline. One driver branch declares its config, DTS,
   shared power/clock/pinctrl/IOMMU dependencies and verification objective.
2. Compile/test branches independently. Never turn on several unvalidated
   drivers merely because they compiled together.
3. Validate individual hardware functions on the real board through a separately
   reviewed loader/initramfs path, then combine and regression-test them.
4. Add postmarketOS user space without changing the shared kernel baseline and
   boot firmware simultaneously; keep NixOS as a comparison target.

Current limitation: this lab does **not** yet provide the postmarketOS package,
initramfs, phone loader integration, rootfs, firmware packaging, driver-specific
runtime tests or a safe flashing/recovery procedure.

## Licenses

New tooling/docs: MIT. Imported DTS: BSD-3-Clause. Kernel changes retain Linux
source licensing (added EFI helper code GPL-2.0-only). See `LICENSE` and source
SPDX notices; this does not relicense upstream Linux.
