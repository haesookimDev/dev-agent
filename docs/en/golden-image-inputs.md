# Golden image candidate build — Draft

[한국어](../ko/golden-image-inputs.md) | English · [MVP progress](mvp-progress.md) · [IMG-001](roadmap-detailed.md)

## Scope

[`prepare.py`](../../infra/images/prepare.py) only verifies inputs and prepares separate copies. [`build.py`](../../infra/images/build.py) and the [Packer template](../../infra/images/ubuntu.pkr.hcl) invoke [`guest.py`](../../infra/images/guest.py) installation/sealing followed automatically by two [`boot.py`](../../infra/images/boot.py) boot checks on an approved dedicated KVM host. **Actual image build/boot/desktop verification of this path and release gates remain incomplete, as does IMG-001.** Existing Worker base-image configuration/execution is unchanged and no automatic rollout occurs.

First obtain reviewed Ubuntu 24.04 **amd64 or arm64** image bytes, a matching-architecture Codex platform package or standalone executable, a Linux browser ZIP and wheels for Runner and all Python dependencies. The agent can acquire and verify public inputs from official sources; users need not supply those files themselves. Check expected hashes against reviewed official distribution/build evidence. Hashing an untrusted file yourself does not authenticate its publisher. The repository does not ship unverified production versions/checksums as a release lock.

## Version 1 input contract

The manifest is UTF-8 JSON, at most 128 KiB. Only the following fields are allowed; duplicate JSON keys, unknown fields and floating aliases (`latest`, `current`, `main`, etc.) are rejected. During input preparation, versions are declarations rather than installability checks.

| Field | Required content |
| --- | --- |
| `schema_version`, `architecture` | Integer `1`, string `amd64` or `arm64`; no mixed architecture or TCG fallback |
| `image_version` | Reviewed fixed image-version identifier |
| `ubuntu_snapshot` | Valid UTC date `YYYYMMDDTHHMMSSZ`; guest APT execution checks actual availability |
| `runner_source_commit` | Lowercase 40-character Runner source Git SHA; wheel-to-source provenance remains a later gate |
| `apt_packages` | Object mapping package names to exact version strings, at most 256 entries |
| `base_image`, `codex`, `browser` | Each an object with `file`, `version`, `sha256`, `size_bytes` |
| `runner_wheels` | 1–64 wheel objects with the same four fields plus `name`; `kelpie-vm-runner` is required and normalized package names must be unique |

Input preparation requires APT names `build-essential`, `ca-certificates`, `dbus-x11`, `git`, `nodejs`, `npm`, `python3.12-venv`, `qemu-guest-agent`, `xfce4`, `xorg`. The builder additionally requires exact versions of `lightdm`, `libnss3`, `libgbm1`, `libasound2t64`, `fonts-liberation`, `x11-utils`, `apparmor`. `x11-utils` supplies `xdpyinfo` to check the actual X display response; AppArmor supplies application-specific sandbox policy while retaining global user-namespace restrictions. This list does not guarantee complete desktop/browser dependencies; actual installation and use must be verified.

`file` is a flat ASCII filename; even case aliases must be unique. Base images use `.qcow2`/`.img` (maximum 64 GiB), browsers `.zip` (2 GiB), wheels `.whl` (256 MiB each); Codex inputs are at most 1 GiB. `size_bytes` is a positive integer and `sha256` is 64 lowercase hex characters. Extension checks do not validate internal formats, architecture or executable safety.

### Complete Codex platform package

The [official Codex CLI guidance](https://learn.chatgpt.com/docs/codex/cli) describes Linux installation; exact platform layouts are verified against actual distribution files. Reviewed `@openai/codex` `VERSION-linux-x64` (amd64) and `VERSION-linux-arm64` (arm64) platform archives are supported. Do not confuse these with the top-level JavaScript wrapper or another architecture. Set `codex.version` to `VERSION`; filenames ending in `.tgz`/`.tar.gz` select the complete-package path. Standalone ELF inputs with other names must also match the manifest architecture. Existing schema-1 amd64 calls remain compatible, with no Worker configuration change or automatic image replacement.

- [`codex_package.py`](../../infra/images/codex_package.py) checks layout version 1/platform metadata and five matching-architecture ELF files (Codex, `codex-code-mode-host`, `rg`, `bwrap`, `zsh`) under `package/`. amd64 uses `vendor/x86_64-unknown-linux-musl/` and ELF machine 62; arm64 uses `vendor/aarch64-unknown-linux-musl/` and machine 183. All files are preflighted before extraction into `/opt/kelpie/codex`, preserving relative layout; `/usr/local/bin/codex` points to the native entrypoint inside it. This does not claim an npm-managed installation or perform login.
- Limits are 100 regular files, 512 MiB total and 64 KiB per metadata file. Path escapes, case aliases, ancestor/file conflicts, links and special/sparse files are rejected. Executable files become `0755`, others `0644`, without setuid/setgid bits. Decompressed reads are bounded too. Other layouts require review/tests before support is added.
- Installation records filenames, sizes, SHA-256 and modes in `/opt/kelpie/codex-inventory.json`. Boot checks reverify every helper and the exact entrypoint link before checking the version. This inventory is not a publisher signature or complete SBOM.

## Running and results

Use a POSIX filesystem and Python 3.12 or newer. Place declared files directly inside a dedicated input root. Replace `/approved/...` below with actual approved paths. Check available disk space first; do not use a production image directory as output.

```sh
image_workspace="$(mktemp -d /tmp/kelpie-image-inputs.XXXXXX)"
python3 infra/images/prepare.py \
  --manifest /approved/image-lock.json \
  --assets /approved/image-assets \
  --output "$image_workspace/bundle"
```

- The output parent must be owned by the current user with mode `0700`. Any existing destination—including an empty directory, file, link or failed bundle—is refused, never automatically deleted or overwritten.
- Only declared single-link regular files are read. Final-component symlinks for the manifest/input root and symlink/hardlink/FIFO/directory inputs are rejected. Approved ancestor paths and same-user processes must remain inside the trust boundary.
- Files are copied in chunks of at most 1 MiB into new `0600` files, checking exact size, SHA-256 and file state before/after copying. Directories are `0700`; undeclared files are neither read nor copied.
- Success writes `files/`, canonical `manifest.json` and `inputs-verified.json`, prints JSON and exits 0. The final record has status `inputs_verified` and **always `release_eligible: false`**. It attests that copied bytes matched the inputs, not successful boot or image release.
- Errors print classifications without private paths/raw details and exit 1. A failed copy may retain a private partial directory without a success record. Do not consume it; fix the cause and retry at a new output path. Cleaning previous partial files is a separate action requiring exact-path/ownership checks.
- Future consumers must require a complete success record and matching manifest hash and **recheck copied-file hashes immediately before use**. Truncated records after power/disk failures, file existence or a previous success log do not authorize use.

## Candidate build on a dedicated host

Run this only as an approved **non-root Linux amd64/arm64 user with accessible `/dev/kvm`**, matching the manifest to the native architecture. Do not run the builder directly on macOS; a Linux arm64 VM that has passed actual KVM verification in the [Mac-local KVM lab](macos-kvm-lab.md) can serve as the dedicated host. Ordinary GitHub runners or emulation do not replace actual KVM evidence. The host needs matching `qemu-system-x86_64` or `qemu-system-aarch64`, `qemu-img`, `ssh-keygen`, `xorriso` and **Packer 1.16.0**. Verify the [official archive SHA-256](https://releases.hashicorp.com/packer/1.16.0/packer_1.16.0_SHA256SUMS), install separately and specify the executable path. The [QEMU plugin](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu), installed by Packer into the private run directory, is pinned to **1.1.6**. No unapproved host access, environment discovery or automatic host dependency installation occurs.

### Additional ARM64 requirements

- Images, Codex, browsers and native wheels must target Linux arm64. Chrome ZIP roots are `chrome-linux-arm64`, distinct from amd64 `chrome-linux64`. Pure Python wheels still require pinned versions, hashes and metadata verification.
- ARM64 APT pins the date directly in `https://snapshot.ubuntu.com/ubuntu/<ubuntu_snapshot>/`. An initial `ports.ubuntu.com` plus `Snapshot:` configuration failed with a snapshot-support error on actual Ubuntu 24.04. The dated URL passed metadata refresh and candidate-version checks for all 16 required packages on the same VM with signature, TLS and expiry checks enabled. Existing amd64 archive/snapshot configuration remains unchanged.
- Minimal Ubuntu hosts also need `ipxe-qemu`. The first actual build failed to initialize the virtio NIC because `efi-virtio.rom` was missing; installing `ipxe-qemu=1.21.1+git-20220113.fbbdc3926-0ubuntu2` on the dedicated host allowed identical device initialization to pass. This ROM is a host QEMU dependency, distinct from the guest APT lock. No NIC, security-check or hardware-acceleration bypass is used.
- The Ubuntu 24.04 host needs the following files from `qemu-efi-aarch64=2024.02-2ubuntu0.9`. The builder verifies hashes, creates private copies and records them in the recipe without writing to the originals. Other versions require reviewed, tested pin changes rather than automatic acceptance.

| File under `/usr/share/AAVMF/` | Size | SHA-256 |
| --- | --- | --- |
| `AAVMF_CODE.no-secboot.fd` | 67,108,864 bytes | `4a4cb7f6d8106bb2a7dd8c763fab14b1810152136fc4304e5b728f0043e84f12` |
| `AAVMF_VARS.fd` | 67,108,864 bytes | `b3b855c5a80310168051164986855692d1bdb06e67619856177965cd87c6774f` |

ARM64 uses `virt`, KVM, host CPU/GIC, a virtio GPU and SCSI seed CD. The first probe starts with a pinned blank VARS copy, not builder-mutated NVRAM; both cold boots retain the same private VARS and disk overlay. CODE is read-only, and neither host firmware nor the original candidate is a writable VM target. amd64 retains q35. Actual Worker ARM64 lifecycle and console integration require separate acceptance; this builder does not complete them.

Packer's `qemuargs` replaces the entire default `-device` list, so the ARM seed CD controller and drive are explicitly attached. Builder/probe VMs with dedicated DMI product markers also retain manufacturer `KVM`: ARM64 [systemd detection](https://github.com/systemd/systemd/blob/v255/src/basic/virt.c) uses DMI rather than x86 CPUID. DMI is descriptive metadata, not acceleration proof. Native host architecture and `/dev/kvm` checks, mandatory QEMU `accel=kvm`, and guest `kvm`/dedicated-product checks remain enforced. A `qemu` result or TCG fallback is not accepted.

### Command

```sh
image_build_workspace="$(mktemp -d /tmp/kelpie-build.XXXXXX)"
python3 infra/images/build.py \
  --bundle /approved/prepared-bundle \
  --output "$image_build_workspace/run" \
  --packer /approved/tools/packer \
  --execute-on-dedicated-host
```

- Missing execution acknowledgement or incompatible host conditions are rejected before staging/VM creation. The flag records the operator's host approval; it does not grant authority or isolate a host. Use a dedicated test host without production services and reviewed inputs only.
- The output parent must be owned with mode `0700`; the normalized output path must be at most 69 ASCII letters/digits/`/_.-`, reserving socket-path room for automatic `boot-check` before building. Only new paths are accepted. Budget space for input copies, Packer caches, a 40 GiB guest disk and the probe's candidate copy/overlay simultaneously.
- The complete input receipt and manifest hash are checked, then every input is verified/copied again. Recipe files and the Runner unit are copied and hashed. Base/output disks must be independent unencrypted qcow2 without backing/external data files, at most 40 GiB virtual size. A `.img` must also contain qcow2.
- Each build generates a temporary SSH key without forwarding the SSH agent or ambient API/SCM/cloud environment. SSH forwarding/VNC bind `127.0.0.1`; the guest-agent socket is inside the private run directory. Build NAT supports package installation; **it does not implement production per-work network isolation**.
- The guest checks Ubuntu 24.04, matching amd64/arm64 manifest architecture, KVM and its dedicated DMI marker before installing. It checks [Ubuntu snapshot](https://snapshot.ubuntu.com/)-pinned APT versions/inventory, hash-locked offline wheels/metadata/`pip check`, Codex ELF/version and browser ZIP boundaries/version. It configures Xfce/LightDM and a Runner disabled until assignment; no browser `--no-sandbox` bypass is used.
- Ordinary automatic APT update timers are masked. Rebuild with a new snapshot/lock, verify, then replace images; never operate this candidate without security updates. Installed inventories remain in guest `/opt/kelpie/apt-inventory.json` and `python-inventory.json`; they are not a complete SBOM.
- Sealing is configured to lock the builder account, remove its dedicated sudo/SSH access, clear SSH host keys/cloud-init seed/machine identity, then power off. Code existence is not evidence of actual reboot or credential non-disclosure.
- The host driver applies command timeouts, a one-hour Packer build deadline and owned-process-group termination. It deletes the temporary key pair on exit, not existing bundles/partial run directories. Physical VM/key/file recovery after host termination or power loss remains a separate gate.
- The build stage records `image_built_unverified`, **`release_eligible=false`**, image/recipe hashes in `candidate.json`. This alone is not CLI success: boot checks below follow automatically. Only complete success returns JSON containing `candidate`, `boot_smoke` and `release_eligible=false`. Raw tool diagnostics are not exposed. Do not reuse failed directories or automatically assign candidates as Worker base images.

## Automatic boot health checks

The `build.py` CLI runs the following in `boot-check/`, with no skip flag. Failure exits 1 without approving any remaining candidate. [`health.py`](../../infra/images/health.py) is installed inside the guest; [`qga.py`](../../infra/images/qga.py) handles private guest-agent communication.

1. Recheck candidate/recipe/manifest records and hashes, copy the candidate independently and create a probe qcow2 overlay. Never attach the original candidate as a writable VM disk.
2. Start a **KVM VM without NIC, VNC or TCP management ports**, using a new cloud-init seed, two vCPUs and 4 GiB. TCG fallback is forbidden. Check the dedicated DMI marker and Unix socket peer against the owned QEMU PID/UID. This offline smoke does not verify production work-network isolation.
3. Follow the [QEMU guest-agent contract](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html): nonce/delimiter synchronization discards stale responses; reads have size/time limits. `guest-exec`, `guest-exec-status` and `guest-shutdown` must already be allowed. Never enable disabled RPCs to pass a test.
4. Check cloud-init completion, sealing record/manifest, initialized IDs, removed/locked builder access, absence of known credential caches, locked APT/wheels/inventories, Runner import/inactivity before assignment, Codex/browser versions, LightDM/Xfce and the X display. This is neither comprehensive secret scanning nor an authenticated Codex job.
5. Run Chromium headless as unprivileged `kelpie` with a disposable profile. The [private CDP-pipe probe](../../infra/images/browser_probe.py) checks the exact browser version, JavaScript calculation/DOM in a `data:` document and normal exit within 24 seconds, closing the command pipe after the close response. No sandbox bypass, external website or debugger TCP port; clean the cgroup descendants and profile. This **does not replace visual review, clicks/keyboard, console input ownership or Computer Use acceptance.**
6. Each boot/check shares one 300-second deadline; shutdown has a 60-second limit. Since successful shutdown RPCs have no response, wait for the QEMU process to exit 0. Failures/timeouts terminate only the created process group and never publish success.
7. After clean poweroff, start the same overlay again. Boot ID hashes must differ and machine ID hashes must match; never record raw IDs. These are two cold boots, not proof of identity isolation between separate VMs, in-guest reboot or host-failure recovery.
8. Recheck that the candidate copy's hash/size is unchanged, then write `boot-smoke.json` last. Its status is `boot_smoke_passed_unreleased`, **`release_eligible=false`**, preserving two guest reports and image/recipe/probe-tool hashes. Signing/SBOM/vulnerability/actual GUI/canary gates remain separate.

For an independent recheck, use `python3 infra/images/boot.py --build-dir /approved/completed-build --output /approved/private/new-probe --execute-on-dedicated-host`. Replace placeholders with actual approved paths, an owned `0700` parent and a new output (normalized full path at most 80 characters). Neither automatic nor independent probes delete existing partial outputs/overlays/seeds. Investigate and clean precisely owned resources separately before retrying at a new path. Missing or truncated boot receipts are failure, not approval.

## Security and unfinished gates

Never put Codex login caches, API keys or SCM/Worker credentials in shared images/input bundles. [Official Codex authentication guidance](https://learn.chatgpt.com/docs/auth) treats `auth.json` like a password. This CLI checks approved-byte identity; it is not a general secret scanner or signature verifier. Production authentication and product approval gates remain unchanged.

Remaining gates: installation/reboot/desktop/browser use and Codex/Runner compatibility with real locked inputs; wheel-to-source provenance and pinned host QEMU toolchain/reproducibility; SBOM/vulnerability reports/signatures; comprehensive credential non-disclosure; actual KVM boot/health probes and canary rollback. An approved Linux/KVM host and reviewed real inputs are required; related feature PRs remain Draft until those gates pass. Ordinary GitHub PR CI does not start expensive VM builds or access production resources.

## Verification record

Implementation `c2ec08e`, file-boundary coverage `ff368cb` and CI integration `85e639f` passed 19 input tests, seven CI-control tests and `make lint`. Full `make test` at `85e639f` with an isolated PostgreSQL database passed: API 1,270 passed/one skipped, Runner 45, Worker/Gateway, Web 125/type checking and 19 image-input tests. The skip is Linux systemd syntax verification unavailable on macOS; Linux CI covers it.

Review found that the JSON parser also implicitly accepted UTF-16/32. The regression first failed for both encodings; `59fda72` enforces UTF-8 and **20 input tests** pass. Input tests take about 0.2 seconds locally and run inside the existing `Python` job. `make test-images` is also part of `make test`; there are no new dependencies, jobs or increased timeouts.

Tests cover actual separate CLI processes, successful copies/permissions, same-sized tampering and real source rewrites during copying, duplicate keys/path escapes/links/FIFOs, write failures, partial-output reuse refusal, concurrent runs and multi-chunk copying. Fixtures are explicitly synthetic bytes, not real Ubuntu/Codex/browser/wheel releases. Actual-use and final PR CI evidence also belong in the PR verification record. No Web UI or native-input behavior changes, so before/after screen verification is inapplicable; VM desktop verification remains unperformed.

At `59fda72`, a dedicated Orca terminal on macOS arm64/Python 3.13.5 ran the actual CLI separately. Sequential checks confirmed four synthetic inputs copied with private modes, exclusion of undeclared files and `release_eligible=false`; a wrong hash exited 1 without a success record; reuse of the partial output was refused. An initial shell-update prompt consumed the first command, which was resent after shell readiness without changing app/shell settings. This is not evidence of a running guest GUI.

Follow-up guest installation/sealing `7b24b20` and host/Packer integration `ce5a6f5` passed **46 `make test-images` tests** (about 1.7 seconds) and `make lint`. Real separate processes checked host refusal, environment filtering, failure cleanup and timeout child-process termination; simulated build commands checked ordering, each failure stage, key cleanup and no promotion. Actual Packer 1.16.0 with QEMU plugin 1.1.6 also passed **full `packer validate` with synthetic inputs**. An actual probe exposed Packer's EOF failure when `/dev/null` was used as configuration; a private `{}` JSON config fixed it. Probe inputs, temporary keys and plugins were cleaned with their dedicated temporary directory; no VM started.

`make test-image-template PACKER=/path/to/packer` checks formatting and **syntax only**. Existing `Go` CI caches the official Packer archive and verifies its pinned SHA-256 every time. It installs no plugin/VM and adds no job/matrix/eight-minute timeout increase. Syntax checking does not replace full plugin configuration validation or actual build/boot. See the PR verification record for this follow-up's full regression and final-head CI.

Follow-ups `c63be0e` (guest checks), `34a16ff` (guest-agent transport) and `0379e48` (automatic two-boot integration) passed **84 of 85 image tests/one skipped** (about 2.3 seconds on macOS), `make lint`, Packer formatting/syntax and full synthetic-input configuration validation. The skip is Linux `SO_PEERCRED` PID verification, run against a real Unix socket in Linux CI. Mac tests also use real Unix sockets for synchronization, fragmented/stale responses, duplicate keys and size/time limits. Two boots, guest state and QEMU shutdown are mocked, not actual VM execution. No Python dependency, CI job or VM CI was added.

Platform verification `86369b4` and guest/boot integration `a73f600` passed **96 of 97 image tests/one Linux-only skip** (2.216s), `make lint`, Packer formatting/syntax and full synthetic configuration validation. Coverage includes complete file preservation, metadata/architecture mismatch, path/link/size limits, legacy ELF compatibility, installed helper mutation and incorrect entrypoint links. Existing CI jobs run these tests without new product Python dependencies.

### Actual public-input verification on 2026-09-10

The following real files were acquired in a dedicated private directory on the Mac. They are not synthetic fixtures, but this is **not guest installation, binary execution or KVM/GUI verification**.

| Input | Pinned target and actual check |
| --- | --- |
| Ubuntu | Downloaded [Noble `release-20260826`](https://cloud-images.ubuntu.com/releases/noble/release-20260826/) amd64 `.img`; matched official `SHA256SUMS`. GPG signature and internal qcow2 inspection remain unperformed. |
| Codex | Compared SHA-512 of the `0.154.0-linux-x64` platform archive and top-level npm distribution against official registry integrity metadata. Actually extracted eight platform files and exercised the install helper/inventory recheck. No authenticated Codex job. |
| Browser | Actual boundary checks/extraction of [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) `153.0.8010.36` Linux64 ZIP. Calculated SHA-256 is in the candidate lock, not claimed as independent publisher SHA-256 attestation. |
| Runner | Built a wheel with `hatchling==1.32.0` from the Git archive at `1fd999fbaf32706d8bbe8f07a803c61135d96ba4`. Compared SHA-256, size and non-yanked status of eight Linux amd64/Python 3.12 dependency wheels against PyPI version metadata. Guest `pip check` and execution remain unperformed. |
| APT | An isolated metadata-only Ubuntu 24.04 container ran `apt-get update` and amd64 `apt-cache policy` for 16 required packages at snapshot `20260909T000000Z`. TLS, GPG and expiry checks remained enabled; this is not guest package installation evidence. |

A separate `prepare.py` CLI using the 12 real amd64 inputs returned exit 0, `inputs_verified` and `release_eligible=false`. Manifest SHA-256: `39e57c6b068eab711f53b5e929460fdf3acb2fea12e685fb505cde5bff106855`. The candidate bundle, public provenance metadata and logs are retained locally; binaries/virtual environments are not committed. No dedicated KVM host was available at that input-verification stage. A Mac-local arm64 KVM lab has since been verified, but its diskless Linux boot does not complete Golden Image build/boot/GUI gates.

### ARM64 input preparation later that day

Twelve actual ARM64 inputs were prepared separately as `20260910-arm64-candidate.1`, manifest SHA-256 `cb6c2a695d04f6f288f2c0b6b062550fe4d7dbbf69efe4cd0ccd5be5e7678970`. Status remains `inputs_verified`, `release_eligible=false`.

| Input | Verified version, size and SHA-256 |
| --- | --- |
| Ubuntu arm64 | `24.04-20260826`, 619,036,160 bytes, `afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f`; matched official checksum |
| Codex arm64 | `0.154.0-linux-arm64`, 122,610,794 bytes, `a2315b5f64bfeaff79b71e0d35505ba8c22cc1e96cab9dc950b614c804105b24`; matched registry SHA-512, non-executing extraction and revalidation of all eight files |
| Chrome arm64 | `153.0.8010.36`, 195,918,275 bytes, `dfc4955719c5d494c8507990506d2d5bed174c31bf89266aa2dc5593c6607e8b`; hash measured from the official CfT distribution |
| PyYAML arm64 | `6.0.3`, Python 3.12 manylinux aarch64 wheel, 775,116 bytes, `9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28`; matched PyPI hash and size |

The other eight pure Python wheels were hash-verified and copied from the previously verified bundle. Git diff confirmed Runner sources were unchanged since `1fd999f`, so the original source SHA is retained. All 16 ARM64 APT candidate versions were checked at the same `20260909T000000Z` snapshot and matched the amd64 candidates. An incorrect Ubuntu byte count in an earlier note was rejected by input preflight; only a new output using the remeasured size and unchanged pinned hash was consumed.

The dedicated Linux VM received Packer `1.16.0` Linux arm64 from an archive whose SHA-256 `cf18f03460d92265d49b56befff333e80641d845822799eab04357c39f75b5d7` matched the official checksum. Executable hashes matched on both sides (`48a5367d3d1d84ebe3740a411b206b8b43143e91604179f517c77d9cad0af69d`), and actual version output was `Packer v1.16.0`. Input/tool preparation remains distinct from actual Golden Image build, boot and visual acceptance.

After ARM installer integration through `3a727a0` and filesystem-clock-independent regression coverage in `6c2cf2e`, actual Ubuntu 24.04 arm64/Python 3.12 passed **all 125 image tests** (2.818s), including the `SO_PEERCRED` check skipped on Mac. Before the fixture correction, rapid consecutive writes shared a modification timestamp, so the mutation test reached a later hash check instead of the intended metadata guard. The test now explicitly changes the actual file timestamp and additionally checks the copied bytes retain the original hash, without relaxing rejection or no-success-record requirements. Sixteen invalid date/path/newline cases for direct installer-helper calls also failed before the fix and passed afterward.

The Packer template at `5fd9219` passed full `validate` against actual ARM inputs, pinned firmware, Packer 1.16.0 and QEMU plugin 1.1.6. This does not prove execution of subsequent template changes. Temporary SSH keys were removed; that validation created no VM. Full regression passed API 1,270/one Linux-systemd skip, Runner 45, Worker/Gateway, Web 125/type checking and Lab 18.

Subsequent actual runs reproduced a missing seed CD attachment and ARM64 KVM identification failure. `11518f9` restores the seed CD; `34ae353` preserves both the dedicated product marker and KVM identity. After first failing the regression checks, `34ae353` passed **all 127 image tests** on actual Linux (2.894s), **126 passed/one skipped** on Mac, Packer syntax checks and `make lint`. Failed VMs and disposable keys were cleaned, with no candidate approval record.

A fresh standard builder run at `34ae353` reported `aarch64`, `kvm` and the dedicated DMI product over actual SSH; its QEMU process also held `/dev/kvm` and KVM VM/vCPU handles. A read-only framebuffer showed the Ubuntu 24.04.4 login screen. **Package installation was still running at this observation; it does not prove sealing, two boots or desktop/browser use.** A Mac Screen Sharing connection failure is recorded separately and is not counted as successful remote input.

### Subsequent build and browser diagnostics, September 10–11

The same standard run completed installation and sealing. Its unreleased qcow2 candidate is 5,073,272,832 bytes, SHA-256 `39958a2014b08a753b0295f033cdf4d0243dd033a6f33909e3fa8d8851d5fcdf`; the receipt remains `image_built_unverified`. The first automatic boot check timed out, so there is **no two-boot success receipt or release approval**. The original candidate was preserved; later experiments used a separate diagnostic overlay.

`fabc600` fixes an actual Guest Agent capture problem: Ubuntu's pinned QGA `1:8.2.2+ds-0ubuntu1.18` kept reporting `exited=false` with stdout-only capture even after the child exited. A separate non-root agent reproduced it. Boolean capture plus a fixed, clean-environment wrapper that discards stderr before execution returned proper results; regression tests passed on Mac and Linux. No guest errors or environment values are forwarded. In the diagnostic VM, sealed identity, removed builder access, package inventories, idle Runner and the Xfce/X display checks then passed; browser launch remained rejected by the user-namespace policy.

`7b46236` fixes a second reproduced bug: the browser timeout killed `runuser` but left 13 Chrome/helper processes. The probe now uses a fresh, unprivileged systemd transient service with an independent 30-second runtime limit, five-second stop deadline, cgroup-wide termination and exact ownership checks. A garbage-collection race is accepted only after confirming the service is gone and its cgroup is absent or empty. Actual Linux synthetic checks covered normal exit, exit 7, detached double-fork children ignoring termination, runtime timeout and the real `systemctl` not-found race; none left a live descendant. [systemd's cgroup termination semantics](https://github.com/systemd/systemd/blob/v255/man/systemd.kill.xml) are not replaced by process-group-only cleanup. At this commit, `make test-images` passed **136/one platform skip on Mac** (2.510s) and **all 137 on Linux** (2.829s); `make lint` passed. Unchanged Packer configuration retains its earlier validation, not a new build claim.

An exact-path AppArmor prototype was tested **only in the diagnostic overlay**, retaining the global user-namespace restriction and Chrome sandbox. This follows Ubuntu's [application-specific namespace policy](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/); an `unconfined` profile is not an extra MAC confinement layer. Sampled renderers had the expected profile, `NoNewPrivs`, seccomp filters and distinct user/PID namespaces. Computer Use observed Xfce and an actual Chrome window displaying JavaScript's computed `4`. The private viewer was read-only: this proves neither desktop input nor product Console authorization.

The standard `--dump-dom` probe still returned no result in a separate 90-second diagnostic, with little CPU activity after startup; GPU and private D-Bus experiments did not resolve it. Chrome 153's [command handler](https://github.com/chromium/chromium/blob/153.0.8010.36/components/headless/command_handler/headless_command_handler.cc) still supports that option, so removal is not the explanation. A private alternative using [CDP pipes](https://github.com/chromium/chromium/blob/153.0.8010.36/content/browser/devtools/devtools_pipe_handler.cc), without a debugger TCP port, checked the exact browser version, created a page, verified its computed DOM and closed the browser in 5.11 seconds; cgroup cleanup passed. This is a prototype, not the final committed browser probe. Its bounded VM was subsequently stopped.

Local evidence includes `arm-browser-cgroup-images-reviewed.log`, `arm-browser-cgroup-linux-reviewed.log`, `arm-browser-cgroup-real-03.log`, `arm-browser-cgroup-gc-real.log`, `arm-browser-headless-timeline-01.log`, `arm-browser-cdp-vm-01.log` and the September 10 18:29 KST Computer Use screenshot. Before promotion, implement and test the final browser probe, exact policy and file integrity checks, rebuild the committed recipe, pass two clean cold boots and verify actual interaction. On September 11, PR #55 was still Draft at `f96635c`; the ARM64 follow-up through `7b46236` had not been pushed or checked by new-head CI. These diagnostics do not increase the MVP completed-stage count.

### Browser probe and sandbox policy implementation, September 11

`2c52b12` integrates the CDP probe into image installation, recipe hashes and boot tooling. It checks the unprivileged user, dedicated VM and disposable directory, then strictly verifies version, DOM and exit using bounded responses and time. A separate-process regression reproduced leaving the command pipe open after the close response; delivering EOF fixes it. In the actual ARM64 diagnostic overlay, the final helper completed normal exit and cgroup cleanup in **14.68 seconds with the viewer connected and 12.01 seconds disconnected**. Earlier delay/exit failures remain recorded; neither deadlines nor sandbox restrictions were relaxed.

`2fa0cdd` integrates [browser integrity and AppArmor policy](../../infra/images/browser_policy.py) into installation, sealing and boot. It checks every file/directory under `/opt/kelpie/browser/` and ancestor ownership/permissions, file hashes/sizes/single links and native ELF architecture, persisting `browser-inventory.json`. The policy is created exclusively at `/etc/apparmor.d/kelpie-image-browser` and grants `userns` to **one exact native Chrome path**. Existing files, loaded-name collisions and disable/complain overrides are rejected. Inventory, policy contents/loading, AppArmor service and global restrictions are rechecked before execution. This `unconfined` policy is neither extra MAC confinement nor a per-UID policy.

As explained in Ubuntu's [Noble policy notes](https://discourse.ubuntu.com/t/ubuntu-24-04-lts-noble-numbat-release-notes/39890), the default profile restricts capability use inside a user namespace rather than necessarily denying its creation. An actual unprivileged negative control entered the default `unprivileged_userns` profile and confirmed network-namespace creation was denied with `EPERM`. Initially expecting plain `unshare --user true` to fail was an incorrect diagnostic expectation; the diagnostic was corrected without relaxing product policy.

Verification at `2fa0cdd`: **168 passed/one platform skip on Mac** (6.222s), **all 169 passed on Linux** (4.394s), `make lint`, and `make test-image-template PACKER=/tmp/kelpie-packer-116.vJqU21/packer`. A Linux fixture umask difference was fixed by explicitly setting initial test-directory permissions, retaining the actual permission checks. The final policy helper reverified **316 entries** and the existing policy in the actual image in 5.88 seconds. Evidence: `cdp-integrated-real-eof-01.log`, `cdp-integrated-real-eof-disconnected.log`, `browser-policy-real-01.log`, `browser-policy-linux-final.log`, `browser-policy-mac-final.log`, `browser-policy-template.log`.

The actual rebuild exposed an integration error: Packer's guest-upload list omitted both new Python modules. `cea2e53` transfers all seven tools and adds a regression detecting omissions. It failed before the fix and passed afterward. Final checks: **169 passed/one skip on Mac** (9.107s), **all 170 passed on Linux** (4.493s), Packer formatting/syntax and `make lint`. Evidence is in `packer-guest-tooling-red.log`, `packer-guest-tooling-mac.log` and `packer-guest-tooling-linux.log`. A standard rebuild of this commit started on September 11 at 10:22 KST; the earlier failed run is not counted as success.

To migrate inputs, resolve an exact `apparmor` version from the pinned snapshot, prepare inputs under a new `image_version`, and **rebuild a new candidate**. Do not edit existing manifests/completion receipts or candidates in place. Adding the verified ARM snapshot's `apparmor=4.0.1really4.0.1-0ubuntu0.24.04.7` produced `20260911-arm64-candidate.1`: 12 files, 17 APT pins, manifest SHA-256 `fa46d223c85df43cbf4781d14262e3c4eb1dc69686668bb302b82dfb7f4d8736`. The standard rebuild has started, but **two new-image boots and actual-input acceptance remain incomplete**. Never promote a failed candidate; rollback must use a separately approved prior image, not the preserved unverified candidate.
