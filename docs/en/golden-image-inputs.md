# Golden image input preparation — Draft foundation

[한국어](../ko/golden-image-inputs.md) | English · [MVP progress](mvp-progress.md) · [IMG-001](roadmap-detailed.md)

## Scope

[`infra/images/prepare.py`](../../infra/images/prepare.py) is a Python-standard-library CLI that verifies declared image inputs and prepares separate copies. It does not download, install, extract, create VMs or roll out Workers. **It is not an image builder, boot test or release approval tool; IMG-001 remains incomplete.** Existing Worker base-image configuration and execution are unchanged.

First obtain approved Ubuntu 24.04 amd64 image bytes, an unpacked Codex executable, a Linux browser ZIP and wheels for Runner and all Python dependencies. Check expected hashes against reviewed official distribution/build evidence. Hashing an untrusted file yourself does not authenticate its publisher. The repository does not ship unverified production versions/checksums as a release lock.

## Version 1 input contract

The manifest is UTF-8 JSON, at most 128 KiB. Only the following fields are allowed; duplicate JSON keys, unknown fields and floating aliases (`latest`, `current`, `main`, etc.) are rejected. Versions are declarations, not checks of actual binary versions or installability.

| Field | Required content |
| --- | --- |
| `schema_version`, `architecture` | Integer `1`, string `amd64` |
| `image_version` | Reviewed fixed image-version identifier |
| `ubuntu_snapshot` | Valid UTC date `YYYYMMDDTHHMMSSZ`; the future builder must check snapshot availability |
| `runner_source_commit` | Lowercase 40-character Runner source Git SHA; wheel-to-source provenance remains a later gate |
| `apt_packages` | Object mapping package names to exact version strings, at most 256 entries |
| `base_image`, `codex`, `browser` | Each an object with `file`, `version`, `sha256`, `size_bytes` |
| `runner_wheels` | 1–64 wheel objects with the same four fields plus `name`; `kelpie-vm-runner` is required and normalized package names must be unique |

Required APT names are `build-essential`, `ca-certificates`, `dbus-x11`, `git`, `nodejs`, `npm`, `python3.12-venv`, `qemu-guest-agent`, `xfce4`, `xorg`. This minimum list does not guarantee complete desktop/browser dependencies. The future builder must verify repositories pinned to the [Ubuntu snapshot service](https://snapshot.ubuntu.com/), the full installed package set and update policy.

`file` is a flat ASCII filename; even case aliases must be unique. Base images use `.qcow2`/`.img` (maximum 64 GiB), browsers `.zip` (2 GiB), wheels `.whl` (256 MiB each); Codex is a single unpacked executable (1 GiB). `size_bytes` is a positive integer and `sha256` is 64 lowercase hex characters. Extension checks do not validate internal formats, architecture or executable safety.

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

## Security and unfinished gates

Never put Codex login caches, API keys or SCM/Worker credentials in shared images/input bundles. [Official Codex authentication guidance](https://learn.chatgpt.com/docs/auth) treats `auth.json` like a password. This CLI checks approved-byte identity; it is not a general secret scanner or signature verifier. Production authentication and product approval gates remain unchanged.

Still unimplemented/unverified: pinned Packer/equivalent builder, package installation/wheel dependency and provenance validation, actual desktop/browser use, Codex/Runner compatibility, SBOM/vulnerability reports/signatures, sealing/reboot/credential non-disclosure, actual KVM boot/health probes and canary rollback. An approved Linux/KVM host and reviewed real inputs are required; related feature PRs remain Draft until those gates pass. Ordinary GitHub PR CI does not start expensive VM builds or access production resources.

## Verification record

Implementation `c2ec08e`, file-boundary coverage `ff368cb` and CI integration `85e639f` passed 19 input tests, seven CI-control tests and `make lint`. Full `make test` at `85e639f` with an isolated PostgreSQL database passed: API 1,270 passed/one skipped, Runner 45, Worker/Gateway, Web 125/type checking and 19 image-input tests. The skip is Linux systemd syntax verification unavailable on macOS; Linux CI covers it.

Review found that the JSON parser also implicitly accepted UTF-16/32. The regression first failed for both encodings; `59fda72` enforces UTF-8 and **20 input tests** pass. Input tests take about 0.2 seconds locally and run inside the existing `Python` job. `make test-images` is also part of `make test`; there are no new dependencies, jobs or increased timeouts.

Tests cover actual separate CLI processes, successful copies/permissions, same-sized tampering and real source rewrites during copying, duplicate keys/path escapes/links/FIFOs, write failures, partial-output reuse refusal, concurrent runs and multi-chunk copying. Fixtures are explicitly synthetic bytes, not real Ubuntu/Codex/browser/wheel releases. Actual-use and final PR CI evidence also belong in the PR verification record. No Web UI or native-input behavior changes, so before/after screen verification is inapplicable; VM desktop verification remains unperformed.

At `59fda72`, a dedicated Orca terminal on macOS arm64/Python 3.13.5 ran the actual CLI separately. Sequential checks confirmed four synthetic inputs copied with private modes, exclusion of undeclared files and `release_eligible=false`; a wrong hash exited 1 without a success record; reuse of the partial output was refused. An initial shell-update prompt consumed the first command, which was resent after shell readiness without changing app/shell settings. This is not evidence of a running guest GUI.
