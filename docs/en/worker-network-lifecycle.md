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

Executor attachment, enforced host/metadata/other-VM denial, allowed egress and existing-connection isolation belong to separate features and MVP release criteria. They neither replace this PR's creation/collision/cleanup/recovery verification nor justify leaving a verified foundation PR in Draft. Predecessor #61 is merged; [PR #62](https://github.com/haesookimDev/dev-agent/pull/62) records final-head CI, review and merge status. [MVP completion](mvp-progress.md) remains 1/7 (14.3%).

## Pre-merge regression fix and actual revalidation — 2026-09-13

`487a337` fixes cleanup adopting a pre-existing exact-identity filter from its reservation alone after provisioning rejected it. `TestRunNetworkRejectedFilterCannotBecomeCleanupOwnership` failed before the fix and passed afterward. Before changing a network/filter, cleanup requires both private creation records, `network.xml`/`filter.xml`, with exact bytes, regular-file type, ownership, a single link and private permissions. Missing/partial/tampered/public records, symbolic/hard links and directories prevent release and external resource mutation. When resources are already absent, partial-file cleanup and normal recovery can continue.

Final source including actual tests: `d5547cb3e8499f35d7b0c1f59335071d17efa697`. `make test-worker` (daemon 9.090 seconds), `make lint`, focused network/concurrent-store/restart race checks (3.490 seconds), and Linux ARM64 `libvirt_integration`-tagged vet passed. The unprivileged Worker on the same dedicated Mac/Lima/libvirt environment passed two actual defined/active network-cleanup cases in 3.33 seconds, existing-filter collision preservation in 1.47 seconds, and two normal-creation/activation-cancellation cases with separate OS-process recovery in 4.67 seconds. New unit regressions passed in that same Linux invocation too.

The collision test first defines a test-owned filter outside the Worker, then verifies provisioning/cleanup rejection, a retained reservation and the unchanged filter. Afterward the test rechecks its own ownership, removes only that fixture, confirms absence and records a synthetic local release. This is neither Worker adoption nor real API/PostgreSQL acknowledgement. Final inspection found no domain, owned network/filter/bridge/binding or dnsmasq leak, while preserving the original inactive `default` network and built-in filter inventory. Test resources/XML were removed and are reproducible; private journals, binaries and existing images were preserved.

Go 1.24.1 Linux ARM64 binary SHA256 `37bb442367ca68fa11826d90d0a35a56bfa7f32e49accc0a01b1a14a6163d252` matched between Mac/execution host and the final committed-source rebuild. [Original actual log](../assets/worker-lifecycle/network-ownership.log) SHA256: `d66ecb26a77dcfc0d939047a9b945d8ab2e3a3229f4cb51f9d9fd5a97bb8251c`. Keep this separate from earlier runs above.

```sh
cd apps/worker
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-ownership.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-ownership.test -test.run '^(TestDedicatedLibvirtNetworkCleanup|TestDedicatedLibvirtNetworkProvisioning|TestDedicatedLibvirtExistingFilterIsNotAdopted|TestRunNetworkRejectedFilterCannotBecomeCleanupOwnership|TestRunNetworkCleanupRequiresPrivateCreationIntent)$' -test.count=1 -test.v
```

Schema 3 is unchanged. Normal records from the previous production provisioner already persist both XML files first and remain compatible. Remaining external resources with absent/damaged creation records now block automatic cleanup and retain journals/reservations. Do not fabricate or delete records to bypass this boundary. Roll back only after compatible-Worker physical cleanup/API acknowledgement; do not recover unresolved collisions with an older binary lacking this rejection boundary. Read-only security review raised check/mutation races under concurrent administrator reconfiguration; those remain limitations of the existing unsupported-host contract. This fix addresses pre-existing-filter adoption reproducible without concurrent administrator changes, not host-wide atomicity. No new dependency, environment variable or CI job was added. Local full `make test` was not repeated for this Worker-only change; browser/GUI/packet checks are inapplicable because no UI or guest NIC changed.

A pre-existing API backup regression exposed by final CI passed actual CLI verification and latest CI in separate [PR #65](https://github.com/haesookimDev/dev-agent/pull/65), then merged. After integration `fca5e482d181284e4de79a1398bebdcc2bb0ce65`, Worker/Runner/Gateway/Web and Host/CI configuration remain identical to actual verification source `d5547cb3e8499f35d7b0c1f59335071d17efa697`; API is identical to verified baseline `0385120011601fc589f7f503f5213057353e8ee2`. Post-integration Worker tests, 51 backup regressions, static checks and language synchronization passed. This API prerequisite is not a new actual Worker execution. Merge PR #62 only after checking its new-head CI and review.
