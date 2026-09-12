# Dedicated CA trust for the Runner control API — 2026-09-13

[한국어](../ko/runner-control-tls.md) | English · [Operations](operations.md#kvm-worker) · [MVP progress](mvp-progress.md)

## Scope and configuration

Runner's `ControlClient` now accepts optional `KELPIE_CONTROL_CA_FILE` for an HTTPS control API requiring explicitly provisioned trust. Certificate-chain, validity and hostname checks remain enabled. There is no `verify=False`, system CA installation or change to model-provider trust settings.

- The default is unset/empty, preserving HTTPX's existing default certificate verification.
- When set, a PEM CA file creates an SSL context scoped to this control API client. There is no retry using system-default roots instead of the explicit CA. A missing file or invalid PEM fails client construction.
- Operators must set the variable in the Runner process environment, for example `KELPIE_CONTROL_CA_FILE=/run/kelpie/control-ca.pem`, using a **path inside the guest**, and provision only trusted certificates in a file readable only by that user. Never commit private keys, ambient host credentials or actual certificates.
- This PR does not implement Worker-to-guest file delivery. Adding the same variable to the Worker's environment does not configure the guest. Including this Runner version in the Golden Image/Worker and delivering the file are subsequent verification work.
- API paths, payloads and lease/correlation headers are unchanged. Existing local HTTP fixtures remain supported; production control traffic must use HTTPS.

Rollback reverts the feature's merge commit and removes the setting. Drain private-CA-dependent runs first; never work around a failure by disabling verification or switching to HTTP. No database migration is needed.

## Verification evidence

The `main` baseline is `d5fb7fc40e3f0d820db1539d5d47792a130707ff`; implementation and required regressions are at `33c5ba87babfe76d410c00a81967c168214d6919`. Only the necessary six-line implementation from integration-branch commit `129f9b1` was reviewed and carried over, with stronger invalid-input and trust-reset tests.

1. Before implementation, **three new regressions failed**: a trusted TLS request still failed, and missing/malformed explicit CA inputs were ignored.
2. After implementation, `make test-runner`: **48 passed**, 0.29 seconds. Real loopback TLS sockets verify successful connection, untrusted-certificate rejection, hostname-mismatch rejection, default trust after removing the setting, and rejection by an independent default SSL context. OpenSSL is used only to generate fixture certificates; no new Python dependency was added.
3. `make lint` and `git diff --check`: passed.
4. Outside the test runner, **six separate `ControlClient` processes** connected to a disposable TLS server on Mac ARM64. Trusted, untrusted, wrong-name, missing-CA, malformed-PEM and unchanged-default-trust cases all passed. Exactly one valid event HTTP request arrived; rejected connections sent no lease headers. [Credential-free receipt](../assets/runner-control-tls/macos-acceptance.json)
5. The owned TLS server, thread, port and temporary certificate/key directory were cleaned up and checked. The private lab script is `check-runner-control-tls.py`; the repository's [repeatable TLS regression](../../apps/runner/tests/test_control_tls.py) also checks the same core boundaries.

This is real TLS **transport-layer** verification. The event response is a fixture, not proof of production API lease authorization, VM boot, guest IP pinning/firewall policy or a complete Runner task. Browser/Computer-use is not applicable because no UI changed. Local full suites for other components such as Worker/API are not applicable to this change; all existing required PR CI checks still run.

The PR records final-head CI, review and merge status. Subsequent documentation changes are checked for identical implementation/test sources. This feature does not increase the seven-stage MVP release completion count.
