# Worker VM command time limits

[한국어](../ko/worker-command-limits.md) | English · [Firmware verification](worker-firmware.md) · [MVP progress](mvp-progress.md)

## Behavior and limitations

Each VM-creation call to `qemu-img`, `cloud-localds` or `virt-install` has a 45-second limit. Earlier caller deadlines are never extended; cancellation/timeout terminates the command's process group. Wait delay is limited to one second. Existing private diagnostics exclude command output, arguments and paths, while retaining `context.Canceled`/`context.DeadlineExceeded` classifications. This uses [Go exec's cancellation/wait contract](https://pkg.go.dev/os/exec#Cmd), with no new dependency or production environment variable.

Libvirt-owned VMs are not terminated merely by cleaning command descendants. Existing [physical cleanup](worker-lifecycle.md) independently bounds verification of VM/disk absence before API/capacity return. Command limits do not implement total-work `budget_minutes`, OS boot/Runner readiness or API lease-expiry enforcement. Process groups are not a sandbox/cgroup boundary against malicious host programs that detach from the group. Trusted host tooling is assumed.

## Final verification — 2026-09-11

Program commit `ce8db6af4e619459923fdac9951d34583638350c`; commit including actual tests `ae81537ae8b6b155614ba123a8f4df02dbdbc253`.

- Two regressions failed before the fix: an unenforced per-command deadline and a descendant writing a witness after parent cancellation. Both passed afterward, alongside earlier-parent deadlines, refusing to start with zero/negative limits, and private diagnostics. The child fixture exits itself after one second even on failure; cleanup never guesses unrelated PIDs.
- `make test-worker` passed (daemon 6.857 seconds), as did `make lint`. Final command/diagnostic `go test -race` passed in 2.658 seconds. Linux ARM64 build-tagged `go vet` also passed.
- Command regressions and **three actual VM cases passed** on dedicated Lima Ubuntu 24.04 ARM64/libvirt 10.0.0/QEMU 8.2.2/KVM on a Mac M4 Pro/macOS 15.7.3. VM cases took 70.64 seconds: terminal 27.27, transition rejection 21.68, and pending-command cancellation after a running VM was created 21.63 seconds.
- The last case observes an actual running VM while the command has not returned and the journal remains prepared, then cancels. Command termination→physical cleanup→synthetic API ACK→durable released→capacity return was verified. No running journal is fabricated afterward.
- Go 1.24.1 Linux ARM64 test binary SHA-256 `72d0b6923604078f039db3092566bf8d21c60c08e17e2446571061b75b3defd6`; recompilation after committing produced bytes identical to the executed binary. [Actual log](../assets/worker-lifecycle/actual-command-limits.log), SHA-256 `af3d4ed7321000a67a2abacbf4e1e804dde93efe717dfb656d66c2e0312daa60`.
- Final domain inventory was empty. Generated blank bases/overlays/seeds/NVRAM and temporary tooling/HTTP processes were cleaned; per-run `0600` ownership journals, existing images and the inactive `default` network were preserved. Removed fixtures are reproducible.

Run actual checks only as an unprivileged user on a disposable Linux ARM64 KVM host.

```bash
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run 'TestVMCommand|TestCommandFailure|TestDedicatedLibvirtExecutorFirmware' -count=1 -v
```

The firmware fixture replaces NIC/graphics with `none`, using blank disks, synthetic HTTP and fake leases. Cancellation stalls the test wrapper after actual `virt-install` succeeds. It does not inject termination during an in-flight internal libvirt RPC, or verify Golden Image/Runner execution, graceful OS shutdown, total VM time budgets, networking/GUI or concurrent work. Browser/keyboard checks are not applicable because there is no UI change.

Predecessor PRs #60/#58 and final-head CI/integration conditions are required, so the change remains Draft. Apply and roll back only after verified shutdown; preserve journals/reservations for unconfirmed cleanup. Hosts exceeding 45 seconds enter failure/cleanup handling rather than silently extending limits or treating incomplete evidence as success. The fixed MVP completion measure remains 1/7 (14.3%).
