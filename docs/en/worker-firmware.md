# Worker ARM64 firmware ownership verification

[한국어](../ko/worker-firmware.md) | English · [Lifecycle](worker-lifecycle.md) · [Progress](mvp-progress.md)

## Change and compatibility

`LibvirtExecutor` explicitly requires KVM and, on ARM64, UEFI with per-run `<WorkRoot>/<lease_id>/nvram.fd` and a SCSI seed CD. Writable UEFI state belongs to the same [durable ownership record](worker-lifecycle.md); lease/capacity return follows shutdown, undefinition and file cleanup. Existing amd64 firmware/seed-bus selection is preserved. Unsupported architectures fail before VM commands.

Firmware is selected from the trusted host's libvirt configuration. ARM64 requires AAVMF and KVM support; selection failures never fall back to BIOS/TCG. This follows the [virt-install 4.1 specification](https://github.com/virt-manager/virt-manager/blob/v4.1.0/man/virt-install.rst) and [libvirt NVRAM specification](https://www.libvirt.org/formatdomain.html#guest-firmware). No new production environment variable, dependency or database migration is introduced. Images and host firmware files are not replaced or modified.

The initial change selected UEFI for all architectures. Review identified a possible conflict with the Golden Image's existing amd64 BIOS contract. Follow-up regressions first failed for amd64/unsupported architectures and passed after limiting UEFI to ARM64. That initial state was not deployed; commit history is preserved.

## Final-code verification — 2026-09-11

Source/test commit: `dafc0dab677756887b2707871e83deb3ab1748da`. Go 1.24.1 Linux ARM64 test binary SHA-256: `da18ebe5803d75b9dad649ec24c8c77b3ffb2452cf06b3c4edfec0f8627b9833`. Recompilation after committing produced bytes identical to the binary actually executed.

- `make test-worker`: passed (daemon 5.314 seconds). `make lint` and Linux ARM64 build-tagged `go vet` passed.
- Regressions: missing owned NVRAM/KVM/SCSI arguments RED→GREEN; preserved amd64 boot contract and rejection of unsupported architectures RED→GREEN.
- Actual dedicated Lima Ubuntu 24.04 ARM64 on a Mac M4 Pro/macOS 15.7.3, libvirt 10.0.0/QEMU 8.2.2/KVM, unprivileged Worker UID: **two cases passed in 48.90 seconds**. Terminal return took 27.24 seconds; API transition rejection after VM creation took 21.62 seconds.
- Exercised actual `LibvirtExecutor` overlay/cloud-init seed creation, ACLs, `virt-install`, running journal and NVRAM path. The domain used KVM with a read-only pflash loader. Shutdown, undefinition and artifact absence preceded synthetic HTTP API ACK, durable released state and exactly one capacity return.
- [Final actual log](../assets/worker-lifecycle/actual-executor-firmware.log), SHA-256 `dc1937eb2cb5b1f067a41fc6c97f1a92346ad03223cc4d9c9459e5e97a556e0d`. Final domain inventory was empty; the originally inactive `default` network remained unchanged.

The initial actual test failed while removing its fixture base after VM release. Libvirt had changed ownership to QEMU, and `/var/tmp` sticky-directory rules prevented Worker unlinking. The fixture now uses a separate Worker-owned directory with exact QEMU search ACLs. Final execution removed only its generated blank bases/overlays/seeds/NVRAM and temporary tools, preserving per-run `0600` ownership journals. The one leftover blank base from the initial failure was also removed by exact path after ownership/non-use checks. These artifacts are reproducible; existing images/user files were preserved. The failed run is not counted as a pass.

## Reproduction and limitations

Run only on a disposable **Linux ARM64** host with real KVM and an empty domain inventory. Root execution is rejected; absent explicit opt-in or a non-ARM64 host produces a skip.

```bash
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run '^TestDedicatedLibvirtExecutorFirmware$' -count=1 -v
```

**The fixture replaces only NIC/graphics arguments with `none`.** It never activates the shared `default` network and uses a blank base, synthetic API and fake lease. It therefore verifies actual firmware/creation/cleanup, not Golden Image OS boot, graceful OS shutdown, Runner execution, actual API/database, Preview/GUI, per-work networking/time limits or full Executor acceptance. Browser/keyboard checks are not applicable because there is no UI change. Actual amd64 execution was not performed here and remains an image-verification gate.

The unmerged ownership changes in predecessor PR #58 are required. Apply only after verified shutdown/draining and retain records/capacity on unconfirmed cleanup. Roll back to the prior binary after verified shutdown; do not start new ARM work with an old Executor that lacks the owned ARM firmware path. Never bypass recovery by deleting records or manually writing released state. The change remains Draft until final-head CI and necessary integration gates pass; the fixed MVP measure remains 1/7 (14.3%).
