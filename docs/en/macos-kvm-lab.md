# Mac-local KVM development lab

[한국어](../ko/macos-kvm-lab.md) | English · [MVP progress](mvp-progress.md)

## Scope and boundaries

Use a supported Apple Silicon Mac without a separate Linux machine: **macOS → Lima/VZ ARM64 Linux → KVM ARM64 work VMs**. The user approved this local development approach on 2026-09-10. Running x86_64 through TCG is not KVM acceptance. Do not silently replace the existing amd64 image contract with ARM64 or lower MVP completion criteria.

The [template](../../infra/lima/ubuntu-arm64.yaml) is a trusted development host, not a work Golden Image. It has outbound NAT access. `networks: []` means no additional networks, not no internet. Future untrusted work must run in separate inner VMs with per-work networking.

- Defaults: 4 CPUs, 8 GiB RAM, 40 GiB sparse disk. Recheck free resources before image builds or concurrent work.
- Disable Mac home/repository/additional-disk mounts, importing existing SSH public keys, SSH Agent/X11 forwarding, Rosetta, containerd and proxy-environment propagation.
- Disable automatic TCP/UDP forwarding for every guest address/port. Lima management SSH is separate and may initially fall back from vsock to a **random Mac loopback port**.
- Dedicated `LIMA_HOME` contains Lima-generated management keys and state. Protect it with mode 0700; never commit it. On macOS, public Lima download caches may remain under `~/Library/Caches/lima`; do not use that cache for secrets.
- Passwordless sudo and the `kvm` group apply only to the **trusted lab-host administrator** `kelpie`, not product work-VM users.

## Initial setup

Require macOS 15+ and Apple's actual nested-virtualization capability check. A model name alone is insufficient. Run this fresh-environment example from the repository root. Do not recreate an existing lab.

```sh
set -eu
test "$(uname -m)" = arm64
xcrun swift -e 'import Virtualization; import Darwin; if #available(macOS 15.0, *) { exit(VZGenericPlatformConfiguration.isNestedVirtualizationSupported ? 0 : 1) } else { exit(1) }'
LAB_ROOT="$(mktemp -d "$HOME/kelpie-lab.XXXXXX")"
mkdir -m 700 "$LAB_ROOT/tools" "$LAB_ROOT/lima"
curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 180 \
  https://github.com/lima-vm/lima/releases/download/v2.2.0/lima-2.2.0-Darwin-arm64.tar.gz \
  --output "$LAB_ROOT/lima.tar.gz"
printf '%s  %s\n' bbdef91774885a0d05f7b048c4eb89ae2bcf3a0c252ae7ca7934e63df76d93c3 "$LAB_ROOT/lima.tar.gz" | shasum -a 256 -c -
tar -xzf "$LAB_ROOT/lima.tar.gz" -C "$LAB_ROOT/tools"
LIMA_BIN="$LAB_ROOT/tools/bin/limactl"
lab() {
  env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C.UTF-8 \
    LIMA_HOME="$LAB_ROOT/lima" "$LIMA_BIN" --tty=false "$@"
}
lab --version
lab validate infra/lima/ubuntu-arm64.yaml
lab create --name=kelpie-kvm --mount-none infra/lima/ubuntu-arm64.yaml
lab list --json
lab start kelpie-kvm --timeout=10m
lab shell --workdir=/home/kelpie kelpie-kvm -- sh -c 'uname -srmo; test -c /dev/kvm'
```

Stop on any failed command. Verify the archive hash before execution. No system-wide Mac installation or Homebrew change is needed. Preserve the original `HOME` value while excluding ambient tokens, SSH agents and proxies. In a new terminal, restore `LAB_ROOT`/`LIMA_BIN` and the function using the **same recorded dedicated path**. Inspect `lab list --json` for absent mounts, enabled nesting and the ignore-all forwarding policy. Lima's `READY` and optional readiness probe alone do not prove an inner KVM boot.

The Ubuntu 24.04 ARM64 cloud image is pinned to `release-20260826`, SHA-256 `afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f`. The actual download matched official `SHA256SUMS` and passed Lima's digest verification. Separate GPG signature verification is not claimed.

## Actual nested-boot check

Make these changes only inside the newly created dedicated Linux host. Do not disable APT signatures or expiry checks. Lab package installation does not replace reproducibly locked Golden Image inputs.

```sh
set -eu
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n apt-get update
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y --no-install-recommends qemu-system-arm busybox-static
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n usermod -aG kvm kelpie
lab shell --reconnect --workdir=/home/kelpie kelpie-kvm -- id
lab shell --workdir=/home/kelpie kelpie-kvm -- sh -c \
  'test ! -e /home/kelpie/lab-kernel && sudo -n install -o kelpie -g kelpie -m 0400 "/boot/vmlinuz-$(uname -r)" /home/kelpie/lab-kernel'
lab copy --backend=scp infra/lima/kvm_smoke.py kelpie-kvm:/home/kelpie/kvm_smoke.py
lab shell --workdir=/home/kelpie kelpie-kvm -- python3 /home/kelpie/kvm_smoke.py \
  --kernel /home/kelpie/lab-kernel --output /home/kelpie/kvm-smoke-01 --timeout 60
```

Reconnect after changing groups. Do not make `/dev/kvm` world-writable or run QEMU as root. Choose a fresh output name for each run; existing directories are never overwritten. Success receipts and serial/QEMU logs stay in the guest output directory; retrieve only needed files using `lab copy`. Receipts record public kernel/BusyBox/probe hashes.

The [probe](../../infra/lima/kvm_smoke.py) starts a 1-CPU/512-MiB inner VM with no disks, NICs or display. It requires QMP `present=true` and `enabled=true`, no block devices, an ARM64 boot message carrying this run's random nonce, and clean poweroff/QEMU exit 0. There is no TCG fallback. The default timeout is 60 seconds, allowed range 1–120. Failures terminate/reap the QEMU process started by this invocation and produce no success receipt. Forced cleanup can add up to five seconds each for termination and reaping. Only temporary QMP sockets are cleaned; evidence directories remain.

## Actual verification on 2026-09-10

Verified code: template `055a61e`, probe `a7413dd`. Subsequent CI/documentation changes do not alter this runtime code. Verify matching file hashes and exact final-head CI in the PR.

| Item | Actual observation |
| --- | --- |
| Mac | M4 Pro, 24 GiB RAM, macOS 15.7.3, Apple API `nested_virtualization_supported=true` |
| Outer Linux | Lima 2.2.0/VZ, 4 CPUs/8 GiB, Ubuntu ARM64, `6.8.0-138-generic`, first boot approximately 12 seconds |
| Boundaries | No guest shared mounts; ignore-all automatic forwarding log; management SSH listener on `127.0.0.1`; no agent/existing-key forwarding |
| Runtime packages | QEMU `1:8.2.2+ds-0ubuntu1.18`, busybox-static `1:1.36.1-6ubuntu3.1` |
| Successful boot | Final probe approximately 5.790 seconds, KVM `present/enabled=true`, no block devices, ARM64 nonce message, `Power down`, exit 0 |
| Failure paths | Access denied before applying KVM group; 1-second timeout; invalid-kernel 10-second timeout. No remaining QEMU or success receipt after timeout. Existing output rejected and prior evidence preserved |
| Probe SHA-256 | `b34003d714878075dd2fe4c1b83ba13306b6a4fad94b30f67d5a8834aad4cc95` |
| Kernel SHA-256 | `a6c429cb79db29b987d138d1e8b2a6c9f0bbad28023145e2db7fe95505e96c9d` |
| Serial SHA-256 | `214d9b626e28a705e27d1059933a7f4220cdcf824bbbef2e2ecfcd3203f9c923` |

Use the 18 policy/protocol tests in `make test-lab` and `make lint`. They run within existing `Python` CI and `make test`, adding no jobs, matrices, VM builds or longer timeouts. Synthetic QMP protocol tests are separate from the actual KVM execution above. This host configuration/CLI change has no web UI, so browser/Computer Use screen validation is not applicable.

Desktop images, libvirt Worker execution, browser/console input, per-work networking and concurrent two-work acceptance **remain unverified**. Next, extend the amd64 image path in [Draft #55](https://github.com/haesookimDev/dev-agent/pull/55) with coordinated ARM64 input/firmware/browser contracts. Do not increase MVP completion based on this host check.

## Stop and recover

`lab stop kelpie-kvm` stops only this dedicated VM, retaining disks and evidence. Resume with `lab start kelpie-kvm --timeout=10m`. Check state after a Mac restart; no autostart service is installed. Roll back this configuration PR and stop the lab if necessary; there is no product DB/API migration. Before deletion/reset, verify the exact dedicated instance and evidence backups and make that decision separately. Never delete the entire home/shared cache.

References: [Lima v2.2.0](https://github.com/lima-vm/lima/releases/tag/v2.2.0), [VZ driver](https://lima-vm.io/docs/config/vmtype/vz/), [Apple nesting capability](https://developer.apple.com/documentation/virtualization/vzgenericplatformconfiguration/isnestedvirtualizationsupported), [QEMU ARM virt](https://www.qemu.org/docs/master/system/arm/virt.html), [Linux initramfs format](https://docs.kernel.org/driver-api/early-userspace/buffer-format.html).
