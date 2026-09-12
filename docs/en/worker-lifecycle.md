# Worker VM ownership and resource release

[한국어](../ko/worker-lifecycle.md) | English

## Enforced ordering

The Worker uses the API-issued Claim lease UUID as its run UUID and synchronizes a private schema-2 ownership record before any VM command. An existing lease directory, including a released one, is never reused. This does not introduce a new work-retry protocol. The process-wide `.worker.lock` prevents overlapping ownership and remains held until execution cleanup and failure reporting finish.

`prepared → running(optional) → cleanup-pending → cleaned → released` uses append-only phase JSON files. Records contain work/attempt UUIDs, resource quantities and timestamps, never lease tokens, assignments, environment values or repository URLs.

1. Verify the actual domain UUID, name, ownership Description and disk paths.
2. Request ACPI shutdown and observe for up to 20 seconds. If necessary, reverify ownership and force-stop only that UUID.
3. Undefine only a confirmed stopped domain. Inspect other domains' current XML and persistent domains' next-boot configuration too.
4. After confirming domain absence and no foreign disk references, remove only the exact run artifacts. Never recursively delete a directory or request libvirt's bulk Storage deletion.
5. Persist `cleaned`, then request API lease release. Return local capacity only after the API acknowledgement and durable `released` record.

Failed cleanup, failed queries, unknown files and ownership mismatches never imply success. Cancelled execution cleanup has an independent 60-second deadline; daemon shutdown joins its executions. Heartbeats retain reservations while cleanup runs. If the API acknowledged release but the local journal write failed, an in-process retry does not repeat that API release.

## Linux file access boundary

The supported host uses Ubuntu's `libvirt-qemu` account and `qemu:///system`. Do not run VMs as the Worker/root account or disable libvirt DAC/AppArmor. Run directories grant **search-only ACL access** to that exact QEMU UID. No directory listing, writing, default inheritance ACL or other-user grants are allowed. Metadata and journals remain Worker-owned `0600` files.

The ACL Mask can make the displayed directory Mode `0710`. The Worker verifies the exact named-UID ACL, not just those mode bits. ACL reads and writes target an open directory FD. Unexpected existing ACLs are not overwritten. Ubuntu's `acl` package must provide `getfacl` and `setfacl`.

libvirt may retain QEMU ownership of readonly seeds after shutdown or leave QEMU-owned NVRAM after failed creation. After domain-absence/foreign-reference checks, only the run's exact `root.qcow2`, `seed.iso` and `nvram.fd` accept that UID with regular-file `0600` permissions and one hardlink. Other owners, links, public files and QEMU-owned metadata are rejected. See [libvirt's host access restrictions](https://www.libvirt.org/drvqemu.html) and [Linux ACL permission semantics](https://www.man7.org/linux/man-pages/man5/acl.5.html).

Administrators must prepare the WorkRoot's parent path and Base Image in dedicated locations accessible to the hypervisor. The Worker does not automatically change user Home or existing image-directory permissions. Default macOS tests cover file, state and concurrency contracts, not Linux ACLs or actual KVM.

## Restart and migration limitations

Before registration, the Worker reads ownership records and physically cleans outstanding runs. For schema-2 records with terminal work, individual Worker authentication allows exact lease/work/resource inspection, another physical-absence check, API reconciliation and durable `released` acknowledgement. A fresh registration must confirm the same Worker, zero active runs and full configured capacity before admission. Missing, ambiguous or redirected responses are not accepted as success. See [restart verification](worker-restart-recovery.md).

Schema-1 records used unrelated random run UUIDs. They remain readable for physical cleanup but are never silently adopted as API leases. Nonterminal work, lost Claim responses, unknown records, invalid credentials and unresolved remote reservations still block admission; a zero heartbeat cannot overwrite them. Recovery currently requires an online Worker. This is terminal-lease recovery, not complete autonomous recovery of every interrupted work item.

Legacy `<WorkID>` directories are neither adopted nor deleted automatically. Drain the Worker and verify existing VM, lease and file ownership before rollout. Do not delete records or manually fabricate `released` files to bypass startup guards. No API Schema/Migration changes. Roll back only after a verified stop while preserving running-work state and journals; older versions do not understand the new records and must not immediately reuse the same WorkRoot.

## Verification scope

```sh
make test-worker
(cd apps/worker && go test -race ./...)
make lint
```

Ordinary regressions run in existing Go CI without extra jobs, matrices or timeouts. Real libvirt tests require explicit acknowledgement and the `libvirt_integration` build tag. Use only a disposable Linux host with an empty domain inventory.

```sh
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run '^TestDedicatedLibvirtCleanupBeforeRelease$' -count=1 -v
```

This test creates a small blank disk, empty cloud-init and a networkless real KVM instead of using an existing image. API responses are synthetic. It tests force-stop after the blank VM does not acknowledge ACPI, and caller cancellation; it does not replace normal OS shutdown, Runner, browser, Console or production API acceptance. Success/failure ownership journals remain under `/var/tmp/kelpie-lifecycle-*`; unconfirmed files are not recursively deleted.

The full Golden Image, per-work networking, actual time-budget enforcement, readiness, nonterminal/lost-Claim recovery and concurrent two-work acceptance remain separate completion criteria. Release progress follows the fixed [MVP record](mvp-progress.md).

### Actual Linux-inside-Mac verification — 2026-09-11

Verified source: `74a8edf35e6d3e3d4ae1b6aefee729473112ea47`. Go 1.24.1 compiled the Linux/ARM64 test binary without CGO; it ran inside the Mac's dedicated Lima Ubuntu 24.04 host. A post-commit rebuild was byte-identical to the executed binary, SHA-256 `8225eb15db7ce27a6f66d541c81e78918f06c4b8f849332b27e43ee0cb64442c`. Environment: libvirt 10.0.0/QEMU 8.2.2, actual KVM and the unprivileged Worker UID.

- [Two real VM cases](../assets/worker-lifecycle/actual-libvirt.log): terminal path 21.98 seconds, caller cancellation 21.60 seconds, total 43.62 seconds, passed. VMs were sequential, both force-stopped after the blank disk did not acknowledge ACPI. This is neither concurrent two-work nor normal OS-shutdown evidence.
- [Recovery of failed-creation NVRAM](../assets/worker-lifecycle/recovery-creation-failure.log) and [post-stop QEMU-owned seed](../assets/worker-lifecycle/recovery-seed-owner.log): both passed in 0.03 seconds. The new binary removed artifacts left by the initial real failures, preserved `cleaned` records and did not invent API-release acknowledgement.
- Under the actual `libvirt-qemu` UID, directory search succeeded while listing/writing and reading `run.json`/`user-data` were denied. Final real domain inventory was empty. Only this test's blank disks, seeds and NVRAM were removed; fixtures can recreate them. Existing images and user data were preserved.
- Mac `make test-worker`, the full Worker race suite, `make lint` and host shell syntax passed. Linux's 24 ownership/recovery regressions were followed by 12 passing final ACL/artifact-owner/cleanup regressions. The PATH-lookup regression failed on Linux before the absolute-path fix and passed afterwards. Record exact final-head GitHub CI separately in the PR.

Actual verification connects the ownership store, ACL, cleanup and daemon resource-release path. Full `LibvirtExecutor` Golden Image boot, Runner control, default networking and ARM firmware configuration still need separate verification; this is not full executor acceptance.
