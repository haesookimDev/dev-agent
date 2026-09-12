# Actual VM deny-all network verification

[한국어](../ko/worker-network-packets.md) | English · [Earlier lifecycle](worker-network-lifecycle.md)

On 2026-09-11, source `071f95f7c96ec16bbf965cf4fcb3b6048122923f` booted the ARM64 Golden Image under actual libvirt/KVM and verified bidirectional Ethernet quarantine on its recorded NIC. **This is not production Executor attachment, allowed-egress policy or Runner completion.**

## Implementation and scope

- `bc45f945b8d114dc04a55b41b6ba5c7f0fbc25a4`: binds NIC XML to the recorded network/MAC/filter, explicitly supplies its IP and disables automatic IP learning. Altered identities are rejected. Network Version 1 still means deny-all; Schema 3 is unchanged.
- The dedicated fixture uses the production RunStore, network provisioner, NIC XML and physical cleanup. Domain XML and QEMU Guest Agent commands are test-only. The production Executor still uses shared `network=default`; this is not an available production isolation path.
- Successful guest loopback UDP send/receive is a positive control. Raw Ethernet transmission bypasses ARP/routing failures, and the test checks increasing counters on the exact TAP's single unconditional DROP rule.
- Twelve frames per direction: six IPv4 (gateway/guest, metadata, three RFC1918 ranges, documentation address), three IPv6 (link-local, ULA, documentation address), ARP, VLAN and a combined MAC/IP-spoofed frame. No external DNS query, real service connection or dependency download occurs.

This uses libvirt's per-NIC filter attachment and IP-learning controls. Actual TAP-scoped ebtables PREROUTING/POSTROUTING DROP counters were observed on installed libvirt 10. This does not establish INPUT/FORWARD behavior for a future TCP/HTTPS allow policy. [Network filters](https://libvirt.org/formatnwfilter.html), [firewall structure](https://libvirt.org/firewall.html)

## Actual results and image preservation

Mac M4 Pro/macOS 15.7.3 hosted dedicated Lima Ubuntu 24.04 ARM64 with libvirt 10.0.0/QEMU 8.2.2/KVM. **One test passed in 109.96 seconds.** A small overlay reused the existing image instead of copying 5GB into approximately 1.9GiB of remaining space.

| Observation | Result |
| --- | --- |
| Guest → host direction | 12 frames sent; exact TAP DROP counter 5 → 17 |
| Host-owned bridge → guest direction | 12 frames sent; exact TAP DROP counter 6 → 18 |
| Physical cleanup | VM/network/filter/bridge/disks/NVRAM/XML absent before synthetic local `released` |
| Existing resources | Original `default` network remained inactive; no remaining binding or owned bridge |
| Base image | Same inode/owner/hash checked while running and after cleanup; original private 0600 ACL restored |

The first 50.09-second packet test passed, but subsequent inspection found that default libvirt DAC handling changed the backing-image owner to QEMU, preventing the Worker from reading it. After checking unchanged bytes and domain absence, the exact file's original owner was restored. That first run is not final image-preservation acceptance.

The final fixture specifies `model='dac' relabel='no'` only on the backing store, temporarily granting the QEMU UID `r--` through an anchored file descriptor. It grants no QEMU write access to the base and does not disable domain DAC/AppArmor. Changed owner, inode, ACL or hash fails verification. The original ACL is restored only after confirmed domain absence. [Backing-store and security-label format](https://libvirt.org/formatdomain.html)

The lab operator temporarily granted QEMU search-only ACLs on four necessary image ancestors and restored all four after verification. No additional other-user/group/world permissions were granted. This is **lab preparation, not a production image-deployment API**. The original image SHA256 is `b2de98bf1725ede0319b2e50c74d4dc9de3499846cdeb3b7ca880b0bd22963a6`.

## Prerequisites and verification commands

Use an acknowledged disposable ARM64 host with no domains, a prepared private Golden Image with QEMU Guest Agent, and narrowly prepared ancestor search permissions. Run as the ordinary Worker user. Only this opt-in fixture uses `sudo -n` to read exact-TAP ebtables counters and send generated frames on the owned bridge. Production Worker `NoNewPrivileges`, privileges and global firewall configuration are unchanged. Never use production credentials or real assignments.

```sh
make test-worker
make lint
cd apps/worker
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-packets.test ./internal/daemon
# Copy to the prepared dedicated Linux host; replace the image placeholder:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only \
KELPIE_LIBVIRT_PACKET_IMAGE=/approved/private/golden-image.qcow2 \
/tmp/worker-network-packets.test -test.run '^TestDedicatedLibvirtQuarantinePackets$' -test.v
```

All passed. After NIC implementation the full Worker daemon tests took 8.846 seconds; final ordinary Worker tests passed from cache, with final Linux-tagged vet, compilation and actual execution separately passing. `make test` was not repeated for this Worker-only change. No Web/native UI changed, so Browser/Computer-use checks do not apply; this OS boot is not final GUI acceptance.

Binary SHA256 `bab5a1fcf355e355acf4f96310ae1e56ea7eb2592e7da5b0040350ae7f2ac5a4` matched on Mac/Linux and a committed-source rebuild was byte-identical. [Unaltered final log](../assets/worker-lifecycle/network-packets.log) SHA256: `9265b3e144cb9a5742aecb5fbc27064dc8be3600ce5da635ce9d35f8aaab4fc4`. No new dependency, CI job or production environment variable was added; this actual-host fixture does not run in ordinary CI.

## 2026-09-13 final-image revalidation after prerequisite merge

Predecessor [PR #62](https://github.com/haesookimDev/dev-agent/pull/62) merged at `2b7e76d84ea56416c39f00dde1ee2bfa298f3b27` after final head `ff6036c0c1d9318db21eea25768cb400c194aad2` passed [all five CI checks](https://github.com/haesookimDev/dev-agent/actions/runs/34713928147), actual verification and review. This separate NIC feature branch integrated those fixes; final actual verification source is `fdd2e08b0f21447f7d9b32cec063c82ce5d69a84`. Compatibility with cleanup's new creation-intent requirement was rerun instead of inferred solely from old logs.

The unprivileged Worker on the same Mac/Lima/libvirt passed **1 actual test/79.80 seconds** with the new ARM64 image candidate. The guest loopback control and 12 frames in each direction passed; exact TAP DROP counters again rose guest→Host 5→17 and owned bridge→guest 6→18. Synthetic local release followed VM stop/undefine and Network/Filter/Bridge/Disk/NVRAM/XML removal. Independent final queries confirmed no domains, bindings, owned bridges or dnsmasq, preserved inactive `default` and standard filters, and only five private single-link journal records.

Image SHA256 is `2fc312a6335a5919f5a0d4e3cfe2e40eae6915cce47b79801f559afc251035d8`, size 5,095,489,536 bytes. Only a small overlay was created, starting with 2.6GiB free. Original inode/owner/hash and restored 0600 ACL were verified; after domain absence, all four ancestor ACLs recorded in a new dedicated receipt were restored. The image, binaries and private ownership journals/receipt remain; only reproducible VM resources created by this test were removed. This does not pass the image candidate's full GUI or release gates.

`make test-worker` (Daemon 8.898 seconds), `make lint`, Linux ARM64 tagged vet/build passed. Binary SHA256 `1bbce2449440107abdc2570c5ed2694f253520197364dbeb2481fb4d0ca28a42` matches Mac/execution Host/committed-source rebuild. The [new original log](../assets/worker-lifecycle/network-packets-run09.log) has SHA256 `9563634028f6e98606af828b1a7509b8af97c28d0e706a4b60a74a5b204856be`. Earlier 109.96-second evidence for the previous image remains separate above. UI/full Executor/API release were not changed or executed; full `make test` and browser checks were not repeated for this Worker-only integration.

## Remaining gates

Verify allowed HTTPS/DNS/DHCP alongside host/metadata/other-VM denial, established-connection quarantine, two concurrent VMs, production Executor guest-reachable control URL/image access/Runner/whole-run budget, and actual API/PostgreSQL release and failure recovery. Do not extrapolate raw deny-all results into TCP-connection, allow-policy or service-isolation acceptance. Never reinterpret Version 1 as an allow policy.

This NIC-foundation PR merges after normal/failure paths, actual packet enforcement/cleanup/image preservation, final-head CI and review are verified. Unrelated production-Executor/allowed-egress/whole-MVP release gates do not keep it Draft, and its own feature verification is not waived. [MVP progress](mvp-progress.md) remains the fixed **1/7 (14.3%)**.
