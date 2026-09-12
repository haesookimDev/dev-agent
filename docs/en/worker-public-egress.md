# Explicit public IPv4 egress for work VMs

[한국어](../ko/worker-public-egress.md) | English · [Logging installation](worker-network-logging.md) · [Guest control](guest-control-bootstrap.md) · [MVP status](mvp-progress.md)

## Behavior and scope

The default still permits only the exact control API HTTPS IP:port. Explicit `logged-public-ipv4` mode permits public IPv4 TCP/UDP/ICMP egress and replies. Per-work NWFilter MAC/IP anti-spoofing, gateway-only ARP, IPv6/VLAN denial, separate NAT networks and ownership checks remain. This provides repository/model **transport**, not completed sealed-image Runner/model execution or UI/GUI acceptance.

RFC1918, loopback, link-local/metadata, CGNAT, documentation/benchmark, multicast and reserved ranges remain denied, along with administrator-supplied management CIDRs, all current Host IPv4 addresses and other ports on the control IP. The [IANA IPv4 registry](https://www.iana.org/assignments/iana-ipv4-special-registry/) informs non-public denials; the entire protocol-assignment and deprecated relay blocks are conservatively denied. This does not classify every globally reachable special-purpose service as private. Operators must also deny their publicly addressed management services.

Only the exact HTTPS control exception precedes management/private denials. Its IP cannot belong to the Host or the current/future work address pool. Use a dedicated API listener, not a shared CDN or HTTP proxy. IP:port filtering is not SNI/HTTP Host authorization.

[Libvirt filtering](https://libvirt.org/formatnwfilter.html) applies L2 denials before connection tracking and combines directional L3 filtering with the NAT network. Evidence identifies the actual rejecting component; tests do not weaken shared Host firewalls. Fresh Host inventories are checked twice, but this is **not a runtime Host-configuration monitor**. Administrators must drain active work before changing addressing, routing or firewall configuration.

## Configuration and activation

Trusted administrators supply these settings through protected `/etc/kelpie/worker.env`, never work/repository content. [.env.example](../../.env.example) keeps egress disabled.

| Variable | Default | Requirement |
| --- | --- | --- |
| `KELPIE_GUEST_INTERNET` | Empty or `disabled` | Only exact `logged-public-ipv4` activates egress; unknown values fail. |
| `KELPIE_GUEST_DNS_IPV4` | Empty | Enabled mode requires canonical public IPv4 DNS, outside Host/control addresses and denied CIDRs. |
| `KELPIE_GUEST_DENIED_IPV4` | Empty | Enabled mode requires administrator management IPv4 CIDRs: canonical network prefixes, lexically sorted, unique and comma-separated. `/0`, whitespace, IPv6 and host bits fail. |

Disabled mode with leftover DNS/denial settings also fails. The combined administrator list, every Host `/32` and control `/32` must fit within 32 prefixes; lists are never silently truncated. Only the configured DNS is added to static cloud-init networking; DHCP and IPv6 remain disabled.

Stop admission and drain existing work before installing the verified [root hook](worker-network-logging.md) on the idle dedicated Host. Production Worker libvirt/kvm access, `NoNewPrivileges`, credential/TLS boundaries and approval gates do not change. Review management CIDRs, DNS and the control endpoint, apply settings, run scoped actual verification, then resume admission. This documentation does not authorize automatic production deployment.

## Durable contract and failure handling

- Schemas 1–4 and network versions 1 (deny-all)/2 (control-only) remain readable. Only enabled runs use Schema 5/network version 3, recording DNS and the combined denial list. No API/DB schema changes.
- Worker verifies installation and reads fixed root-owned receipts without root locks, nft commands or configurable privileged RPCs. Worker policy fields do not become root logging identity.
- `network-start.json` durably records run/boot identity before activation. New root `pending`/`active` evidence is required before NIC creation; real NIC attach/update hooks recheck kernel rules/handle and journal readiness.
- Physical VM/network/filter/bridge removal and matching root `stopped` evidence precede lease return. Start intent survives artifact cleanup. Exact private `network.xml`/`filter.xml` creation intent is independently required; logging receipts never authorize adopting foreign resources.
- Missing/changed evidence or uncertain crash boundaries retain reservations and receipts and refuse admission/return. Automatic recovery immediately after intent or between `pending` and `active` is not implemented. Do not delete evidence, edit schemas or force-return resources to bypass checks.

`stopped` proves physical cleanup, **not continuous lossless audit completeness**. The hook records new-connection headers, not payloads, URLs or DNS questions. Runtime logger/journald loss, overload detection, retention/janitors and safe partial-start reconciliation remain release conditions documented in the [logging limitations](worker-network-logging.md).

## Rollback

Changing configuration never rewrites recorded run policy. Stop admission, confirm physical cleanup/root stop evidence/lease returns for Schema 5 runs, set mode to `disabled` and clear both DNS/denial settings, then restart and verify control-only behavior. Old Worker binaries cannot read Schema 5: preserve records and recover using a verified compatible version instead of converting/deleting them. Hook replacement requires separate idle maintenance preserving original inodes, bytes and records.

## Repeatable verification

### Actual verification — 2026-09-13

The [pinned JSON evidence](../assets/worker-public-egress/macos-acceptance.json) records production `727ae08`, fixture `0596ba2` and binary hashes. Public DNS and an actual HTTPS shallow clone passed in **47.83s** inside the local Mac's Lima/ARM64 KVM Guest. Kernel journal checks matched the exact clone destination, DNS and independent control TLS. No credentials or repository code were used.

Six forged/forbidden frames in each direction produced TAP DROP `0→6` and `4→10`; eight forbidden TCP probes produced `6→14`; unsolicited UDP after a receive-positive-control produced owned NAT bridge REJECT `0→1`. Separate read-only checks verified three root lifecycle receipts, handle/fingerprint and stopped table absence. A regression failed when unrelated public HTTPS traffic was allowed to count as clone evidence, then passed after restoring exact matching.

VM/network/filter/bridge/disks and temporary veth/namespace were removed. Original image SHA256/0600 ownership, four ancestor ACLs and hook inode/hash/ownership were restored. Existing root receipts and new audit evidence were preserved. Only test processes selected UID/GID 1000 with existing libvirt/kvm groups; account/socket permissions and Worker units were not persistently modified. No UI changed, so new screen verification is inapplicable; full GUI/Runner/API acceptance remains separate.

Worker tests/static checks, focused race tests and Linux ARM64/amd64 tagged build/vet passed. This feature's PR requires final-head CI/review/actual-evidence checks and a normal merge, separately from remaining whole-MVP gates.

### Commands and fixture

Run `make test-worker`, `make lint`, focused race tests and Linux ARM64/amd64 `libvirt_integration` builds/vet. Regressions cover disabled/invalid settings, whole-pool control rejection, private creation ownership, missing/tampered root evidence, failed starts and cleanup-before-return.

`TestDedicatedLibvirtInternetClone` requires an acknowledged empty ARM64 KVM Lab, explicit Golden Image/root hook, `KELPIE_LIBVIRT_TEST_ACK=disposable-host-only`, enabled settings above and explicit credential-free HTTPS test origin/IP. Guest checks public DNS and an HTTPS shallow clone of `octocat/Hello-World`. It uses a fresh directory without credentials/user Git configuration and never executes repository code. [Git connection-address configuration](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpcurloptResolve) pins this DNS result while retaining TLS hostname validation, so journal evidence must match the exact clone destination. Independent control TLS traffic cannot substitute for clone evidence.

Checks include raw IP/MAC/ARP/VLAN/IPv6 rejection, management/metadata TCP denial, a separate namespace/veth receive-positive-control and unsolicited UDP denial, root receipts and physical cleanup. The new veth is collision-checked and removed only after matching ifindex/MAC/alias; existing Host routes/firewalls are not changed. Root fixture observation is not production Worker privilege, and its local release is synthetic rather than an actual API return. Existing CI Go kernel checks/tagged builds remain; no additional VM job or production secrets are introduced.
