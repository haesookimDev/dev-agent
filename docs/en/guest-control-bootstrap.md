# Per-work VM control bootstrap

[한국어](../ko/guest-control-bootstrap.md) | English · [Operations](operations.md#kvm-worker) · [MVP progress](mvp-progress.md)

## Behavior and boundaries

Worker does not copy its host-only endpoint into the guest. The `libvirt` executor uses an explicit guest HTTPS origin and pinned IPv4, owned network/MAC/filter and static address. It does not fall back to shared `network=default`. New Schema 4/Network Version 2 records store the control exception; existing Schema 3/Version 1 stays deny-all.

Only the pinned control IPv4/TCP port and narrowly required ARP/return traffic are allowed. DNS, DHCP, IPv6, general internet, host services, metadata and other task addresses are not authorized. A control IP on a Worker interface or anywhere in the task allocation pool is rejected. This is an **IP:port boundary**, not SNI/HTTP Host inspection. Use an exclusive API-only listener, never a shared CDN, generic proxy or a port serving other services. Concurrent host network reconfiguration and VM creation are unsupported.

Startup is `ownership record → network/seed → VM → Runner TLS/lease authentication → analyzing → clone`. Runner validates the API's work ID, integer version and status before requesting `provisioning → analyzing`; Worker no longer performs that transition. A successful `virt-install` is not readiness. One 180-second deadline covers OS startup and observing the initial control connection. Invalid state, stopped VM, read failure and timeout enter the failure path. Fast execution failure or cancellation goes to cleanup without a false ready event.

Clone has a 120-second default limit. Normal exit, error, timeout and cancellation all clean the clone's own process group. Output containing credential-bearing URLs is not retained; only exit codes are reported. Runner re-reads current execution state/version when failing, with one conflict re-read. It does not overwrite cancellation, approval waiting or delivery states, or release leases itself. Worker remains responsible for physical cleanup before API release and local capacity return only after the API acknowledgement.

Worker likewise fails only execution states (`provisioning/analyzing/implementing/verifying`) after an error. It preserves approval, input, feedback, budget waiting and delivery states. If the VM has been cleaned while work remains in one of these protected nonterminal states, its lease and local reservation remain retained. The terminal-only API release policy is not bypassed; automatic recovery here remains part of the unfinished active-run recovery work. Invalid claim states/versions are rejected before host commands or seed generation.

## Configuration contract

| Location / variable | Default and rollout |
| --- | --- |
| Worker `KELPIE_CONTROL_URL` | Existing host endpoint; never a fallback for guest configuration. |
| Worker `KELPIE_GUEST_CONTROL_URL` | No default; required for libvirt. HTTPS origin without user information, path, query or fragment. |
| Worker `KELPIE_GUEST_CONTROL_IPV4` | No default; required for libvirt. Canonical IPv4 of the exclusive control listener. DNS names are pinned in guest `/etc/hosts`. |
| Worker `KELPIE_NETWORK_POOL` | `10.240.0.0/16`. Reserves a collision-free `/30` from a validated dedicated private pool. |
| Worker `KELPIE_GUEST_CONTROL_CA_FILE` | Empty uses guest default public PKI. An optional private CA must be an absolute path to a Worker-owned regular, single-link, `0600` file, at most 64 KiB/eight valid signing CA certificates. Symlinks, private keys and other content are rejected. |
| Generated guest `KELPIE_CONTROL_CA_FILE` | `/run/kelpie/control-ca.pem` when private trust is configured; `kelpie`-owned `0600`, scoped to ControlClient. System/model CA configuration is unchanged. |
| Generated guest `KELPIE_CONTROL_BOOTSTRAP` | Always `1` from the new Worker. Empty preserves standalone Runner compatibility; other values are rejected. Removing the flag to bypass the new libvirt path is unsupported. |

Never commit secrets, certificates or private keys. Only explicitly supplied CA certificates cross this boundary; ambient host trust/credential directories are not searched or copied. Operators provision read-only QEMU access to the immutable image. Only that backing source disables dynamic DAC relabeling; normal VM/writable-overlay DAC and AppArmor boundaries remain. Disk reservations smaller than the base image are rejected before resource creation.

## Rollout, compatibility and rollback

**Drain and upgrade Worker and the Bootstrap-capable Runner image together.** Older Runner images cannot implement the new seed protocol; they are not counted as a successful connection and may fail at the deadline. Mock Executor and standalone Runner without the flag preserve their prior behavior. No API schema or database migration is introduced.

Old Workers cannot recover Schema 4. Before rollback, use the new Worker to clean owned VMs/networks and confirm API acknowledgement/local `released`. Never delete records or rewrite them as Schema 3 to bypass recovery checks. Even released Schema 4 records are rejected by old Workers: retain verified inactive records in a protected separate archive and give the old Worker a new empty work root. If resource absence is uncertain, keep the new Worker and its records for recovery instead of rolling back.

## Verification scope

Repeatable actual tests are the [Worker/VM](../../apps/worker/internal/daemon/libvirt_runner_api_integration_test.go) and [API/PostgreSQL](../../apps/api/tests/test_guest_runner_postgres.py) fixtures. They use production Executor NIC/seed generation and the real API in the dedicated Mac/Lima ARM64 environment. Current Runner source executes through a temporary guest systemd override; this is not verification of a newly sealed Golden Image.

Prepare a dedicated lab with empty domain inventory, read-only image access, an isolated PostgreSQL `KELPIE_TEST_POSTGRES_URL`, and a private lab JSON. Its fields specify the fixture's explicit acknowledgement, SSH config/host, ARM64 test binary, base image, unused forwarding port and guest-visible Mac host address. Each invocation uses fresh work, CAs and credentials; run `trusted`, `untrusted-ca`, `wrong-hostname` and `cancelled` individually. An environment-gated skip is not an actual verification pass.

```sh
KELPIE_TEST_LIBVIRT_RECOVERY_CONFIG=/absolute/private/lab.json \
  .venv/bin/python -m pytest -q \
  'apps/api/tests/test_guest_runner_postgres.py::test_actual_guest_control_bootstrap_preserves_tls_and_release_gates[trusted]' -s
```

GitHub CI compiles and vets `libvirt_integration` code in the existing Go job but does not run VMs. Actual verification uses the dedicated environment above; host credentials or QEMU permissions are not provided to public CI.

Authenticated execution-cancellation verification uses the real lease-scoped transition API. The administrative cancellation API currently supports only unassigned queued work; **this is not user-interface cancellation of a running task**. That separate MVP condition remains open.

Control bootstrap does not complete general cloning, model execution, GUI, preview/console, total VM budgets or concurrent two-work acceptance. General internet egress is deliberately denied by this policy; its successor will use a separate feature branch, verification and PR.

## Actual acceptance — 2026-09-13

Verification used production source `9d1c56f798837a2543756eb7f2049a62d6233231` and test source `f6bfdff11761a3d69e80515c683838796ca344cd` in the dedicated environment above. The [receipt](../assets/guest-control-bootstrap/macos-acceptance.json) records image, binary, fixture and private-log SHA-256 values and per-run differences. Subsequent CI/documentation commits do not change production code.

| Actual path | Observed result | Duration |
| --- | --- | --- |
| Correct CA/hostname | TLS/lease authentication, Runner-owned `analyzing`, then intentionally denied clone and `failed` v4. Eight forbidden TCP attempts; actual TAP DROP 0→8. | 93.19s |
| Wrong CA | Certificate verification error in the current Runner invocation; zero API HTTP/lease-header requests; initial-connection deadline then `failed` v3. | 203.13s |
| Wrong hostname | Hostname verification error in the current Runner invocation; zero API HTTP/lease-header requests; `failed` v3. | 206.48s |
| Authenticated execution cancellation | Actual API cancellation immediately after `analyzing`; Runner error does not replace `cancelled` v4; zero `failed` transitions. | 89.80s |

Every path checked that the domain, owned network/filter, bridge, writable disk, seed, network config and other owned artifacts were absent **immediately before** the API release request. Actual PostgreSQL recorded one release, followed by local `released` after acknowledgement. The base image hash, inode, owner and `0600` ACL were preserved; temporary ancestor access was restored. The 24 pre-existing filters and inactive default network remained. Owned servers, SSH forwarders, CA/key files and fresh test databases/roles were cleaned.

Initial failures remain recorded. SSH health-check wildcard expansion was corrected with argument quoting. Observed cloud-init timing failures changed only fixture waiting to 60 seconds/a 90-second command context; the production 180-second deadline and acceptance assertions remain. Hostname rejection passed with the earlier wait settings, explicitly identified by binary in the receipt. Three synthetic regressions reproduced pytest reprinting private values on failure, then passed with fixed diagnostics. The final trusted run uses both fixture corrections.

`make test` and `make lint` passed: API 1196/157 conditional skips, Runner 102, Web 125/type checking, Worker/Gateway and Lab 18. After the diagnostic regressions were added, `make test-api` passed again with **1199/157**, alongside Ruff. Worker state/claim/ACK regressions, focused race checks and Linux ARM64/amd64 tagged compilation/vet passed. Earlier actual ARM firmware checks (three paths/78.91s) and preservation of a pre-existing control filter (1.55s) also passed; blank firmware VMs are not combined into full guest-execution evidence.

Independent security review identified the state-overwrite and early claim-validation issues, which were corrected with regressions. Follow-up review of production code and actual fixtures found no further blockers. New browser/native-screen verification is not applicable to this backend-only feature; this does not waive the unfinished GUI, full-image or concurrent-work gates above.
