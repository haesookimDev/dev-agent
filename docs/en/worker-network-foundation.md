# Per-work network foundation verification

[한국어](../ko/worker-network-foundation.md) | English

This is **unconnected foundation code** at source `50ada438156ea596cb07d317911bf12f715e194b`, verified on 2026-09-11. The Executor's `network=default` is not replaced yet. This does not prove actual packet isolation or MVP completion.

- `392d3922b169f42a25e039e3dc930adc3c49ed4f`: derives dedicated names/bridge/MACs from a validated lease UUID and proposes a /30 from an RFC1918 pool. Conflicting existing runs/excluded prefixes, inconsistent inventories and exhaustion fail closed. The caller must collect current host inventory and serialize allocation with durable creation.
- `6cd8cdc5a380bce696f545e31cdf43b7a11b6520`: generates network XML with a fixed DHCP target, ownership metadata and disabled IPv6, plus filter XML unconditionally dropping Ethernet in both directions. NAT/port isolation alone does not deny host access. [libvirt Network](https://libvirt.org/formatnetwork.html), [Filter](https://libvirt.org/formatnwfilter.html)
- `50ada438156ea596cb07d317911bf12f715e194b`: opt-in validation against installed libvirt schemas. It does not define/activate networks or filters, run VMs or change firewall rules.

## Verification

`make test-worker` (final full daemon run 6.587 seconds), `make lint`, focused network tests/race checks (1.272 seconds) and Linux ARM64 build-tagged `go vet` passed. Full local `make test` was not repeated for a Worker-only change; UI/browser checks do not apply.

The final network tests and both libvirt 10.0.0 schemas passed as the unprivileged Worker on dedicated Lima Ubuntu 24.04 ARM64 hosted by Mac M4 Pro/macOS 15.7.3 (schema total 0.01 seconds). Only `libxml2-utils` 2.9.14+dfsg-1.3ubuntu3.8 was added to that lab for the existing validator (288kB). No product dependency, CI job or production configuration was added.

```sh
cd apps/worker
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network.test -test.run 'TestDedicatedLibvirtNetworkSchemas|TestRunNetwork' -test.v
```

Final test binary SHA256: `7c921cd17cdb97df864b366c0951cecd7f2ebb117f513912ebb1f6fbd1924478`, matching on Mac and execution host. [Execution log](../assets/worker-lifecycle/network-schema.log) SHA256: `e1b34385bc84c9f509ab59ea693d9cf6d1f03bac58c97c7b91f40d95b9b7c491`. Temporary XML was removed, domain inventory remains empty and the original default network remains inactive.

## Remaining gates

Durable network ownership, host collision checks, Executor wiring, allowed-egress policy, host/metadata/other-VM denial, existing-connection isolation, exact network/filter cleanup, restart recovery and actual two-VM packet checks remain. Generated XML is not evidence of applied libvirt filtering. This foundation has no production rollout or data migration; until connected, its new functions do not change Executor behavior. Keep it unmerged and in integration, with release completion fixed at 1/7 (14.3%).
