# Durable per-work network cleanup

[한국어](../ko/worker-network-lifecycle.md) | English · [Earlier schema-only evidence](worker-network-foundation.md)

Verified on 2026-09-11 at source `26476bebcc385cedbbc8259ec239803680d68246`. This follow-up owns and cleans networks, filters and bridges in explicit fixtures. **The production Executor still uses `network=default`; no guest was attached in this verification.** It is not packet-isolation or full-Executor acceptance.

## Changes and recovery contract

- `fe336092b780e3c76c340ec2f663ced7ddf1c91e`: serializes run-store creation, reads and phase changes. Concurrent readers cannot observe a partially written record; same-phase transitions remain idempotent.
- `75450595619a252ccc42ca88efa8a6de6eb6b103`: `CreateNetworked` serializes subnet allocation with durable Schema 3 creation. The immutable network identity is bound to the lease UUID before host side effects. Unacknowledged reservations and unresolved legacy runs block reuse. Caller-supplied exclusions are not proof that host inventory was actually collected.
- Cleanup checks current/inactive network and foreign-domain XML, exact UUID/name/ownership/configuration, filter references, bindings and network ports. After domain absence, it stops/undefines only the owned network, removes its filter and confirms bridge absence before deleting run artifacts. Unknown or reappearing resources retain the journal/reservation and block release; an orphan bridge is never deleted by name alone.
- Schema 3 keeps the existing API lease binding. Synthetic HTTP recovery tests verify cleanup before registration/reconciliation and renewed admission. The actual network fixture's `released` marker is a local test acknowledgement, **not** an API/PostgreSQL release.

Schema 1/2 bytes remain readable and cannot acquire network authority by appending a field. Production `Create` still writes Schema 2; no existing records are rewritten. After adopting Schema 3, an older binary rejects those records and blocks recovery. Roll back only after the compatible Worker confirms physical cleanup and API acknowledgement; preserve journals and do not delete them to bypass admission.

## Actual Linux verification

Mac M4 Pro/macOS 15.7.3 hosts dedicated Lima Ubuntu 24.04 ARM64 with libvirt 10.0.0. Unprivileged final-source tests passed both defined and active network cases in **3.17 seconds**. Network/filter/bridge absence, preserved pre-existing network inventory and durable acknowledgement were checked. Final read-only inspection found no domains, owned filters/bindings/bridges or dnsmasq processes; the original default network remained inactive.

```sh
make test-worker
make lint
cd apps/worker
go test -race ./internal/daemon -run 'TestRunNetwork|TestRestartReconcilesTerminalLeaseBeforeAdmission|TestRunStoreConcurrent' -count=1
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-cleanup.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-cleanup.test -test.run '^TestDedicatedLibvirtNetworkCleanup$' -test.v
```

All listed checks passed: final full Worker daemon 8.193 seconds, focused race checks 2.728 seconds. `make test` was not repeated for this Worker/host-package change; no API, Runner, Gateway or Web behavior was changed. Browser checks do not apply. No new CI job was added, and these opt-in host checks do not run in ordinary CI.

Final binary SHA256 `4ec90de4a95c33b57ec3059b6fd283cf4bbb7b5830a8b010dc63d58594e82472` matched on Mac and Linux, and rebuilding committed source was byte-identical. [Unaltered final log](../assets/worker-lifecycle/network-cleanup.log) SHA256 `26052f725efe8bf530600e5976df896db14119f815affda605a707e676a80e0a`.

Actual failures were retained and corrected, not treated as passes:

- libvirt canonicalizes element ordering and omits default IPv6 attributes; active NAT adds the default port range. `c111212bfb9e801f8b63cc10a626b4c20693dd5e` compares the full XML structure while normalizing only exact supported defaults. Extra addresses/options, nondefault ranges and modified rules still fail closed. [Network XML](https://libvirt.org/formatnetwork.html), [virsh](https://libvirt.org/manpages/virsh.html)
- The dedicated host lacked `dnsmasq`. `d947e396d273ce714a89e32aa5b27a703903aba4` adds `dnsmasq-base` to the Ubuntu host installer, required by libvirt DHCP. Only that package (2.91-0ubuntu0.24.04.1, 893kB installed) was added to the lab; no global DNS service was enabled. Existing hosts need the package before network activation. No Go dependency or environment-variable contract changed.
- Earlier preserved inactive and active fixtures were successfully cleaned by separate recovery test processes (0.90/1.43 seconds). These earlier recovery checks are distinct from the final two-case log; they do not prove full Worker/API recovery or VM packet filtering.

## Follow-up: host inventory and controlled creation

At source `ebb3d8e9f0c58195d075ed1fef614ce6982f607f`, actual read-only Linux inventory passed in 0.07 seconds: 2 interfaces, 8 excluded IPv4 prefixes and 2 current/inactive XML views. It includes all IPv4 routing tables, default-route gateways, host addresses and inactive libvirt network/domain identity conflicts. Missing/malformed/unsupported inventories fail closed; zero-value snapshots are not evidence of availability. IPv6 inventory validation does not imply IPv6 packet enforcement.

At source `c53fea9ecf6d48d2473550ff0e343311845bfa46`, the network provisioner persists private exclusive `filter.xml`/`network.xml`, verifies quarantine, recollects collision inventory, defines/starts the exact network, and checks its actual bridge MAC/address. Existing resources/files are not overwritten or adopted. Its caller must bind the durable run to the lifecycle and join creation before cleanup. External administrator changes are not made atomic by these snapshots; concurrent host reconfiguration is not supported.

Both actual normal creation and cancellation immediately after successful activation passed in **4.61 seconds**. Each used a separate OS process to reopen the retained journal, physically clean resources/XML, and write a synthetic local release. Final inspection found no owned network/filter/bridge/binding/dnsmasq leak, and preserved the original inactive default network. This is still **no guest NIC, no actual packet test and no API/PostgreSQL acknowledgement**. The production Executor does not call this provisioner yet.

```sh
cd apps/worker
go test -race ./internal/daemon -run '^TestRunNetwork' -count=1
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-provision.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-provision.test -test.run '^TestDedicatedLibvirtHostNetworkInventory$' -test.v
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-provision.test -test.run '^TestDedicatedLibvirtNetworkProvisioning$' -test.v
```

Latest full `make test-worker` passed (daemon 8.195 seconds), followed by the final immutable-record guard and final focused race check (1.989 seconds); `make lint` and final tagged Linux vet passed. No API/Web change, new Go dependency, new CI job or production environment variable was added.

Inventory binary SHA256: `db5785ab442fb54f2a002aabbbac2cd3d06d196bb94f37fc58f4ddb9507997ca`; [inventory log](../assets/worker-lifecycle/network-inventory.log): `fd50c4787085f8ef176892e84b8a1c621211796ad6b1f8e860add7e125cb56cc`. Provisioning/recovery binary: `d51015e87a2f7dc83fc2c93b73f9bd62ebb7283d2a6d8eea625d35e78218b459`; [provisioning log](../assets/worker-lifecycle/network-provision.log): `03c8a3748dafd9e8b8ebf7392c76c64857f0876332290e94b17ccf028f231fe3`. Both binaries matched between Mac/Linux and byte-identical committed-source rebuilds. The logs are preserved from those separate runs, not a claim that both ran in one final binary invocation.

## Remaining gates

Executor attachment, enforced host/metadata/other-VM denial, allowed egress and existing-connection isolation remain. Verify those with actual concurrent VMs before release. This verification snapshot precedes PR submission and has no exact-head GitHub CI result; keep the work unmerged and record subsequent CI in the PR. [MVP completion](mvp-progress.md) remains 1/7 (14.3%).
