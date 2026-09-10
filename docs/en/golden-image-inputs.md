# Golden image candidate build — Draft

[한국어](../ko/golden-image-inputs.md) | English · [MVP progress](mvp-progress.md) · [IMG-001](roadmap-detailed.md)

## Scope

[`prepare.py`](../../infra/images/prepare.py) only verifies inputs and prepares separate copies. [`build.py`](../../infra/images/build.py) and the [Packer template](../../infra/images/ubuntu.pkr.hcl) invoke [`guest.py`](../../infra/images/guest.py) installation/sealing followed automatically by two [`boot.py`](../../infra/images/boot.py) boot checks on an approved dedicated KVM host. **Actual image build/boot/desktop verification of this path and release gates remain incomplete, as does IMG-001.** Existing Worker base-image configuration/execution is unchanged and no automatic rollout occurs.

First obtain reviewed Ubuntu 24.04 amd64 image bytes, a Codex Linux x64 platform package or standalone executable, a Linux browser ZIP and wheels for Runner and all Python dependencies. The agent can acquire and verify public inputs from official sources; users need not supply those files themselves. Check expected hashes against reviewed official distribution/build evidence. Hashing an untrusted file yourself does not authenticate its publisher. The repository does not ship unverified production versions/checksums as a release lock.

## Version 1 input contract

The manifest is UTF-8 JSON, at most 128 KiB. Only the following fields are allowed; duplicate JSON keys, unknown fields and floating aliases (`latest`, `current`, `main`, etc.) are rejected. During input preparation, versions are declarations rather than installability checks.

| Field | Required content |
| --- | --- |
| `schema_version`, `architecture` | Integer `1`, string `amd64` |
| `image_version` | Reviewed fixed image-version identifier |
| `ubuntu_snapshot` | Valid UTC date `YYYYMMDDTHHMMSSZ`; guest APT execution checks actual availability |
| `runner_source_commit` | Lowercase 40-character Runner source Git SHA; wheel-to-source provenance remains a later gate |
| `apt_packages` | Object mapping package names to exact version strings, at most 256 entries |
| `base_image`, `codex`, `browser` | Each an object with `file`, `version`, `sha256`, `size_bytes` |
| `runner_wheels` | 1–64 wheel objects with the same four fields plus `name`; `kelpie-vm-runner` is required and normalized package names must be unique |

Input preparation requires APT names `build-essential`, `ca-certificates`, `dbus-x11`, `git`, `nodejs`, `npm`, `python3.12-venv`, `qemu-guest-agent`, `xfce4`, `xorg`. The builder additionally requires exact versions of `lightdm`, `libnss3`, `libgbm1`, `libasound2t64`, `fonts-liberation`, `x11-utils`. The latter supplies `xdpyinfo` to check the actual X display response. This list does not guarantee complete desktop/browser dependencies; actual installation and use must be verified.

`file` is a flat ASCII filename; even case aliases must be unique. Base images use `.qcow2`/`.img` (maximum 64 GiB), browsers `.zip` (2 GiB), wheels `.whl` (256 MiB each); Codex inputs are at most 1 GiB. `size_bytes` is a positive integer and `sha256` is 64 lowercase hex characters. Extension checks do not validate internal formats, architecture or executable safety.

### Complete Codex platform package

Following the npm distribution route in the [official Codex CLI guidance](https://learn.chatgpt.com/docs/codex/cli), a reviewed `@openai/codex` `VERSION-linux-x64` platform archive is supported. Do not confuse it with the top-level JavaScript wrapper package or another architecture. Set `codex.version` to `VERSION`; filenames ending in `.tgz`/`.tar.gz` select the complete-package path. Existing amd64 ELF inputs with other names remain supported. There is no schema/Worker configuration change or automatic image replacement.

- [`codex_package.py`](../../infra/images/codex_package.py) checks layout version 1/Linux x64 metadata and five amd64 ELF files (Codex, `codex-code-mode-host`, `rg`, `bwrap`, `zsh`) under `package/`. All files are preflighted before extraction into `/opt/kelpie/codex`, preserving relative layout; `/usr/local/bin/codex` points to the native entrypoint inside it. This does not claim an npm-managed installation or perform login.
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

Run this only as an approved **non-root Linux amd64 user with accessible `/dev/kvm`**. Do not substitute local Mac or ordinary GitHub runner VM execution. The host needs reviewed `qemu-system-x86_64`, `qemu-img`, `ssh-keygen`, `xorriso` and **Packer 1.16.0**. Verify the [official archive SHA-256](https://releases.hashicorp.com/packer/1.16.0/packer_1.16.0_SHA256SUMS), install separately and specify the executable path. The [QEMU plugin](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu), installed by Packer into the private run directory, is pinned to **1.1.6**. No unapproved host access, environment discovery or automatic host dependency installation occurs.

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
- The guest checks Ubuntu 24.04 amd64, KVM and its dedicated DMI marker before installing. It checks [Ubuntu snapshot](https://snapshot.ubuntu.com/)-pinned APT versions/inventory, hash-locked offline wheels/metadata/`pip check`, Codex ELF/version and browser ZIP boundaries/version. It configures Xfce/LightDM and a Runner disabled until assignment; no browser `--no-sandbox` bypass is used.
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
5. Run Chromium headless as unprivileged `kelpie` with a disposable profile; verify the DOM after JavaScript executes in a `data:` document. No sandbox bypass, external website or debugging port; clean the profile afterward. [DOM smoke](https://developer.chrome.com/docs/automation-and-testing/headless-cli) **does not replace visual review, clicks/keyboard, console input ownership or Computer Use acceptance.**
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

A separate `prepare.py` CLI using the 12 real inputs returned exit 0, `inputs_verified` and `release_eligible=false`. Manifest SHA-256: `39e57c6b068eab711f53b5e929460fdf3acb2fea12e685fb505cde5bff106855`. The candidate bundle, public provenance metadata and logs are retained locally; binaries/virtual environments are not committed. Public-input acquisition is separate from approval/access to a dedicated KVM host. The latter is missing, so build/boot/GUI gates and Draft status remain.
