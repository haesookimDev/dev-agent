# Secret injection and rotation

[한국어](../ko/secret-management.md) | English

The API uses environment and file implementations of the `SecretProvider` interface. A configured file takes precedence over its environment value and **never falls back on a file error**. Deployments without file settings retain their existing behavior. Never use development default secrets in production.

## Configuration contract

| API environment variable | File-path environment variable | Read timing |
| --- | --- | --- |
| `OIDC_CLIENT_SECRET` | `OIDC_CLIENT_SECRET_FILE` | Every authorization-code exchange |
| `WORKER_SHARED_SECRET` | `WORKER_SHARED_SECRET_FILE` | Every Worker/Gateway authentication request |
| `GITHUB_WEBHOOK_SECRET` | `GITHUB_WEBHOOK_SECRET_FILE` | Every webhook signature verification |
| `SLACK_SIGNING_SECRET` | `SLACK_SIGNING_SECRET_FILE` | Every Slack command signature verification |
| `SLACK_BOT_TOKEN` | `SLACK_BOT_TOKEN_FILE` | Every notification/file-upload operation |
| No environment variable for key contents | Existing `GITHUB_PRIVATE_KEY_PATH` | Every GitHub App JWT signature |

New `_FILE` settings default to empty strings. Set an absolute file path, not a secret value, and grant the API process read-only access. Files must be UTF-8 and at most 64 KiB; only trailing CR/LF characters are removed. Internal PEM newlines are preserved. Empty files, NUL, invalid UTF-8, non-regular files, and access errors are rejected. Never put secrets in command arguments, image layers, Git, or artifacts.

Contents are not cached: files are reopened on every use. Finish a temporary file on the same filesystem and atomically replace the destination instead of writing in place. Symlink replacements take effect on the next read. Multiple HTTP requests within one Slack upload use the same token read at its start.

## Deployment

On local/VM deployments, have the secret manager populate an access-controlled directory owned by a dedicated service account with `0400` or `0600` permissions. Mount the directory read-only into containers. For example, the API section of a deployment-specific Compose override can contain the following. The repository's default Compose remains an isolated development demo.

```yaml
services:
  api:
    environment:
      OIDC_CLIENT_SECRET_FILE: /run/secrets/kelpie/oidc-client
      GITHUB_WEBHOOK_SECRET_FILE: /run/secrets/kelpie/github-webhook
    volumes:
      - /secure/kelpie-secrets:/run/secrets/kelpie:ro
```

This example covers only secret delivery. Production also requires `AUTH_MODE=oidc`, HTTPS, authorization policy, and the remaining [operational configuration](operations.md). Create and distribute actual secret files through an approved secret manager and remove old plaintext environment values.

- Kubernetes: mount a Secret volume directory and configure the paths above. `subPath` mounts do not receive automatic updates, and volume updates are not guaranteed to be immediate. [Kubernetes Secrets documentation](https://kubernetes.io/docs/concepts/configuration/secret/)
- Vault: configure Vault Agent templates to render dedicated files and point the application at those paths. Refresh intervals, Vault authentication, and lease lifetimes remain the agent/operator's responsibility. [Vault Agent template documentation](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template)

This implementation adapts files injected by those platforms. It does not connect directly to Kubernetes/Vault APIs or provision either platform. Deployment verification on an actual cluster or Vault service is still required.

## Rotation, failure, and recovery

1. Prepare new credentials at the provider. Account for in-flight requests when scheduling revocation of the old credential.
2. Have the secret manager atomically replace the file. No API restart is required.
3. Verify acceptance of the new value and rejection of the old value with test requests, then revoke the old provider credential. Never print the values to screens or logs.
4. If a file cannot be read, affected API requests return `503`, `Cache-Control: no-store`, and `{"detail":"configured secret is unavailable"}` without paths or contents. A missing OIDC secret file does not downgrade authentication to a public client.
5. Restore a valid file and recheck requests against the same process. Do not recover by clearing the setting and reverting to development defaults.

Webhooks validate only the current file value. Dual-secret acceptance and zero-downtime distributed rotation are not implemented. Worker/Gateway clients still read environment tokens at startup, so **shared-secret rotation requires coordination with those clients**. Per-worker credentials, individual revocation/quarantine, and client reloading are follow-up SEC-001 work. Existing user-session and work-lease contracts are unchanged.

No schema migration is needed. To roll back the API, first prepare secure secret injection supported by the old version in a maintenance environment with restricted traffic. Do not expose an older version that ignores file settings with development defaults.

## Verification and remaining scope

- `make test-api` covers file precedence, atomic and projected-volume rotation, fail-closed errors, rotation through the OIDC cache, and webhook/Slack/GitHub consumers.
- `test_secret_runtime.py` runs real Uvicorn/SQLite/HTTP processes: rotate → old value 401 → new value accepted → missing file 503 → recovered without restart. It also checks that generated test tokens do not occur in API logs or the database.
- The five supported plaintext secret settings are excluded from default Settings repr/serialization. This does not guarantee secret-free logging, process memory, or crash dumps generally.
- OIDC/Slack network consumer tests use mock providers. Actual external accounts, complete event/artifact/crash-dump/cloud-init scanning, per-worker credentials, and production secret policy remain unfinished. SEC-001 as a whole is not complete.
