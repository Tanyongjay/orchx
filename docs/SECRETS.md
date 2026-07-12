# Secrets

This document is the operator's playbook for the
`{{ secret.x }}` substitution that every orchx
descriptor can use. It covers the security model, the
three backends orchx ships today, and what the v0.4
line adds on top.

## TL;DR

* Set `ORCHX_SECRET_<name>` in the environment before
  running orchx. The descriptor's `{{ secret.<name> }}`
  is replaced at step-execute time, never at load time.
* The resolved value is never persisted: it does not
  appear in the descriptor model, the SQLite run log,
  the event stream, the dashboard, or the operator's
  shell history (unless you `set -x`).
* For a real production deployment, the v0.4 line
  adds HashiCorp Vault, AWS Secrets Manager, and
  Kubernetes-native secret backends. The descriptor's
  syntax is unchanged.

## The security model

orchx's secret model is "last-mile resolution". The
template `{{ secret.db_password }}` is treated as
ordinary text by the loader. The engine then walks
the descriptor's resolved string tree and substitutes
the secret at the very last moment — right before the
command is sent to the transport.

Why this matters:

* A `plan` run never consults the vault. You can render
  a descriptor's DAG on a developer laptop with no
  vault access.
* A failed step doesn't leak the secret into a retry
  log, an error message, or a crash dump.
* A SQLite `runs` row contains the literal
  `{{ secret.db_password }}` token, not the value.
  Anyone with read access to the SQLite file sees the
  shape of the secret ("a password exists") but not
  the value.
* A WebSocket event (`status: failed,
  message: "exit=1"`) never contains a resolved
  secret because the executor's `att.message` is
  derived from the transport's stderr, which is set
  after the secret has been substituted into the
  command. The command itself is not in the event
  log either.

The lock-down test that proves all of this is in
`tests/test_secret_template.py`. It is the most
important test in the suite: the existence of a
resolved secret anywhere on disk would be a security
regression, so the test fails the build if it ever
happens.

## What the descriptor author writes

In a YAML descriptor:

```yaml
- id: db.schema
  type: sql
  on_host: db
  sql: |
    CREATE USER {{ secret.db_user }} LOGIN PASSWORD '{{ secret.db_password }}';
```

The `{{ secret.x }}` token is a Jinja-style template
that the engine resolves. The path component (`x`)
must be a flat name — `secret.a.b` is rejected because
the secrets store is flat, not nested.

The default filter is supported:

```yaml
cmd:
  - /bin/sh
  - -c
  - "DB_PORT={{ secret.db_port | default('5432') }} postgres-cli -h db"
```

If `ORCHX_SECRET_db_port` is unset, the engine uses
`5432`. If it's set, the engine uses the value.

## What the operator sets

The default backend reads secrets from the
environment. Set each name as `ORCHX_SECRET_<NAME>`:

```bash
export ORCHX_SECRET_db_host=db.internal
export ORCHX_SECRET_db_name=oauth_svc
export ORCHX_SECRET_db_user=oauth_svc_ro
export ORCHX_SECRET_db_password='replace-me-in-vault'
orchx deploy descriptors/sample_oauth_service.yaml \
    --target ssh://orchx-deploy@host:22
```

The descriptor's `{{ secret.db_password }}` is replaced
with `replace-me-in-vault` only at the moment the
`db.schema` step's SQL is sent to the PostgreSQL
transport.

`shellcheck` or `set -x` does not reveal the resolved
value because the engine substitutes in-memory; the
shell never sees the substituted command.

## The three backends in v0.3

orchx ships three secret backends, selected by
`ORCHX_SECRETS_BACKEND`:

### `env` (the default)

Reads from `os.environ`. Suitable for:

* Developer laptops where the secret values are in
  `~/.zshrc` or `~/.bashrc`.
* CI pipelines that inject `ORCHX_SECRET_*` from a
  secret store (GitHub Actions secrets, GitLab CI
  variables, etc.).
* A long-lived systemd unit that sets
  `Environment=ORCHX_SECRET_*` in the unit file.

```bash
export ORCHX_SECRETS_BACKEND=env
```

### `file`

Reads from a JSON file. Each key in the JSON object
is a secret name; the value is the secret value.

```bash
export ORCHX_SECRETS_BACKEND=file
export ORCHX_SECRETS_FILE=/etc/orchx/secrets.json
```

```json
{
  "db_password": "replace-me-in-vault",
  "api_key": "f7e8c1..."
}
```

The file must be `0600` and owned by the user running
orchx. orchx does not check this for you; it's a
typical "secrets file" deployment pattern.

### `memory`

Test-only. The vault is populated in-process from a
test fixture, and is cleared on test teardown. The
production use case for `memory` is "I'm writing
tests and I want to inject a fixture without setting
up a file or env var". The orchx test suite uses
this heavily.

```python
from orchx.secrets import set_memory_vault
set_memory_vault({
    "db_password": "test-value",
    "api_key": "test-key",
})
```

## What v0.4 adds

The v0.4 line adds three more backends, all
opt-in via `ORCHX_SECRETS_BACKEND`:

### `vault` — HashiCorp Vault

The HashiCorp Vault backend reads secrets from a Vault
KV-v2 path. The descriptor-side `{{ secret.x }}` is
unchanged.

```bash
export ORCHX_SECRETS_BACKEND=vault
export ORCHX_VAULT_ADDR=https://vault.internal:8200
export ORCHX_VAULT_TOKEN=hvs.abcdef
export ORCHX_VAULT_MOUNT=secret   # the KV-v2 mount point
export ORCHX_VAULT_PREFIX=orchx/ # all paths are ${PREFIX}${name}
```

Each `{{ secret.x }}` resolves to
`vault kv get -mount=$MOUNT $PREFIX/x`. Token
authentication only in v0.4.0; Kubernetes and AWS
auth methods come in v0.4.1.

### `aws` — AWS Secrets Manager

Reads from AWS Secrets Manager via boto3.

```bash
export ORCHX_SECRETS_BACKEND=aws
export ORCHX_AWS_REGION=us-east-1
# Uses the ambient AWS credentials chain (env vars,
# instance profile, ~/.aws/credentials, etc.)
export ORCHX_AWS_PREFIX=orchx/   # all names are ${PREFIX}${name}
```

Each `{{ secret.x }}` resolves to the `SecretString`
field of the `orchx/x` secret.

### `k8s` — Kubernetes-native

Reads from a Kubernetes Secret in the same namespace
as the orchx pod.

```bash
export ORCHX_SECRETS_BACKEND=k8s
export ORCHX_K8S_NAMESPACE=orchx
# Uses the in-cluster service account; no token needed.
```

Each `{{ secret.x }}` resolves to the `data["x"]`
field of the `orchx-secrets` Secret, base64-decoded.

## What's NOT in scope for secrets

* **Secret rotation** is a property of the backing
  store (Vault, AWS, k8s), not orchx. orchx reads the
  value at step-execute time, so a rotated secret
  is picked up on the next deploy without any orchx
  config change.
* **Secret usage audit** is also the backing store's
  job. Vault's audit log is the canonical place.
* **Secret auto-injection into the descriptor at
  load time** is a deliberate anti-feature: the
  loader must not be able to see the values, so the
  pattern `{{ secret.x }}` survives a `git` of the
  descriptor without leaking.
* **Cross-region failover** is a property of the
  backing store, not orchx.

## Threat model

| Threat | Mitigation |
|---|---|
| Operator leaves terminal logged in | orchx's resolved value lives in process memory only; the SQLite file contains `{{ secret.x }}` not the value |
| Operator's laptop stolen | The descriptor is in `git`; secrets are in Vault / AWS / k8s; nothing useful is on the disk |
| Compromised orchx host | The vault token (if any) is the only thing of value; rotate the token and the attacker loses access |
| Compromised target host | orchx substitutes the value into the command; the value is in the target's process listing for the duration of the call. A log-tampering attacker on the target could see the value. This is the standard SSH trust model; mitigate with `ForceCommand` or a bastion. |
| Compromised target host, persisted | The resolved value is not in any on-disk file on the target (orchx only writes what the descriptor tells it to). If the descriptor writes a config file containing `db_password = {{ secret.db_password }}`, then yes, the value lands on disk — but that's the descriptor author's choice. |
| Vault itself compromised | Out of scope; rotate everything |

## Diagnostics

* `ORCHX_SECRETS_DEBUG=1` prints each `{{ secret.x }}`
  lookup at step-execute time, with the name, the
  source backend, and the cached vs. fresh status.
  The value is never printed. Set this if a step
  fails with "secret x not found" and you want to
  confirm the lookup actually ran.
* `orchx doctor` (planned for v0.4) will run a
  connectivity check against each configured backend
  and print a one-line PASS/FAIL per secret.

## Where the orchx code lives

The secret machinery is in `src/orchx/secrets.py`
(about 200 lines) and `src/orchx/descriptor/loader.py`
(secret-template parsing). The executor's last-mile
substitution is in `src/orchx/steps/steps.py`
(`_resolve_payload_secrets`). The lock-down tests are
in `tests/test_secret_template.py` and
`tests/test_secrets.py`.
