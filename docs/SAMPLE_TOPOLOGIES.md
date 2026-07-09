# Sample Topologies

OrchX ships **five** sample descriptors that exercise topologically
different stacks. They share **no platform-specific assumptions**
in the engine — the engine is YAML-agnostic, transport-agnostic, and
host-agnostic; everything system-specific lives in the descriptor
and (for real hosts) in the transport implementation.

## Side-by-side

| Concern | `sample_webapp_erp.yaml` | `sample_oauth_service.yaml` | `sample_containerized_saas.yaml` | `sample_hr_service.yaml` | `sample_settle_eod.yaml` |
|---|---|---|---|---|---|
| **Host** | Windows (IIS, COM, SQL) | Linux (systemd, PostgreSQL) | Linux (docker compose) | Linux (Python venv, uwsgi, supervisor) | Linux (cron, Python venv) |
| **Roles** | web, db | web, db | control, app, db | app | control, db |
| **Service model** | long-running IIS site | long-running systemd unit | 3 long-running containers (app, worker, db) | long-running uwsgi process under supervisor | **no long-running service** — cron job |
| **Web server** | `iis-site` | `command: [systemctl, ...]` | `command: [docker compose up -d ...]` | `command: [uwsgi ...]` (started by supervisor) | n/a |
| **Native bridge** | `com-register` / `com-unregister` | none | none | none | none |
| **Database** | `sql` against SQL Server | `sql` against PostgreSQL | `sql` against PostgreSQL | `sql` against PostgreSQL | `sql` against PostgreSQL (ledger) |
| **Package** | zip (`package` step) | tarball (`package` step) | docker pull (`command` step) | pip wheel into venv (`command` step) | pip wheel into venv (`command` step) |
| **Healthcheck** | `http://.../health` | `tcp://127.0.0.1:7700` | 3× `tcp://` (app HTTP, app admin, worker metrics) | `http://.../healthz` (via uwsgi) | `command: [python -c 'import settle_eod']` (wheel-import smoke) |
| **Reversal** | `rev:bridge.register.x86`, `rev:iis.site.create` | `rev:svc.stop` | `rev:worker.up`, `rev:app.up` | `rev:supervisor.start`, `rev:supervisor.stop` | `rev:cron.write` |
| **Uses secrets** | no | no | no | **yes** (db_password via {{ secret.db_password }}) | **yes** (db_name + db_password) |
| **Steps total** | 10 | 11 | 15 | 11 | 10 |
| **Forward runnable** | 8 (2 are rev:) | 10 (1 is rev:) | 13 (2 are rev:) | 9 (2 are rev:) | 8 (1 is rev:, 1 sibling cron.env.write) |
| **Upgrade style** | stop → install → start | stop → install → start | **rolling** (new worker up before old one is stopped) | stop → install → start | install → write cron line (idempotent) |

## What the engine treats the same

For all five descriptors:

  * The same loader parses the YAML.
  * The same planner produces a DAG and auto-wires `rev:` pairs.
  * The same executor dispatches steps in topological order, retries
    on failure, skips downstream steps on upstream failure, and
    runs the corresponding reversal on a forward failure.
  * The same MockTransport records every action and supports
    chaos injection through `--chaos '{"host":[…]}'`.
  * The same CLI (`orchx plan`, `orchx deploy`) drives everything.
  * The same dashboard renders the timeline with a 🔐 indicator
    next to events whose step_id touches the vault
    (sample_hr_service, sample_settle_eod).

## What is *not* shared

  * The transport implementation. For real hosts, the Linux
    descriptors target an `ssh://` URI; the Windows one targets
    `winrm://`. The engine is oblivious to which.
  * The actual on-host commands. The IIS site creation step runs
    `New-Item IIS:\Sites\…`; the systemd step runs
    `systemctl start …`; the docker-compose step runs
    `docker compose up -d`. Each is delivered to the host by a
    transport; the engine does not care which shell they live in.

## Secret resolution

Three of the five descriptors (`sample_hr_service.yaml`,
`sample_settle_eod.yaml`, and the OAuth descriptor with manual
secrets config) reference the orchx vault via `{{ secret.<name> }}`
tokens. The vault is consulted at step-execute time only — never
at descriptor-load time — so resolved values never appear in:

  * the descriptor model in memory
  * the SQLite run log
  * the dashboard event stream
  * test fixtures

The dashboard surfaces a 🔐 indicator next to events whose
step_id touches the vault. The flag is computed from the source
descriptor at server-side; the dashboard never sees a resolved
value.

See `tests/test_secret_template.py` for the lock-down tests
that prove no resolved value lands anywhere persistent.

## Running them

```bash
# Validate all five descriptors
orchx plan descriptors/sample_webapp_erp.yaml
orchx plan descriptors/sample_oauth_service.yaml
orchx plan descriptors/sample_containerized_saas.yaml
orchx plan descriptors/sample_hr_service.yaml
orchx plan descriptors/sample_settle_eod.yaml

# Deploy against the mock (no real host touched)
orchx deploy descriptors/sample_webapp_erp.yaml      --target mock://local
orchx deploy descriptors/sample_oauth_service.yaml   --target mock://local
orchx deploy descriptors/sample_containerized_saas.yaml --target mock://local

# HR + Settle need vault values; supply them via env vars
export ORCHX_SECRET_db_host=db.internal
export ORCHX_SECRET_db_name=hr_svc
export ORCHX_SECRET_db_user=hr_ro
export ORCHX_SECRET_db_password='correct-horse-battery-staple'
orchx deploy descriptors/sample_hr_service.yaml      --target mock://local
orchx deploy descriptors/sample_settle_eod.yaml      --target mock://local

# Failure path on any descriptor
orchx deploy descriptors/sample_oauth_service.yaml   --target mock://local \
    --chaos '{"local":[{"action":"command","exit_code":1,"fail_times":99}]}'
```

The OAuth descriptor's failure injects a `command` action failure.
The webapp-ERP descriptor's typical failure is `package` or
`iis-site` — see `tests/test_executor.py` for examples.

## Adding a sixth topology

The pattern is straightforward:

  1. Copy one of the sample descriptors.
  2. Rename `system.code` (must be lowercase letters / digits / `_`).
  3. Replace the `steps:` with what your stack actually needs.
  4. If you need a step type we don't ship yet, add it to
     `src/orchx/descriptor/models.py`, an adapter in
     `src/orchx/steps/steps.py`, and a method in `Transport`.
  5. Add the descriptor path to `tests/test_descriptors.py`'s
     parameterised list so CI locks the topology down.
  6. If your descriptor uses secrets, seed the test fixture
     with stub `ORCHX_SECRET_*` values so the mock transport
     can complete the run.

If a step's *semantics* are unique to your platform (e.g. systemd
`daemon-reload`, `firewall-cmd`, `iptables`), prefer the generic
`command` step with a clear `cmd:` rather than inventing a new
step type. Step types are for things that are common enough to
warrant a typed surface (IIS sites, COM bridges, SQL execution);
one-off shell calls belong in `command` / `powershell`.