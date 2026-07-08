# Sample Topologies

OrchX ships two sample descriptors that exercise topologically
different stacks. They share **no platform-specific assumptions**
in the engine — the engine is YAML-agnostic, transport-agnostic, and
host-agnostic; everything system-specific lives in the descriptor
and (for real hosts) in the transport implementation.

## Side-by-side

| Concern | `sample_webapp_erp.yaml` | `sample_oauth_service.yaml` |
|---|---|---|
| Host | Windows (IIS, COM, SQL) | Linux (systemd, PostgreSQL) |
| Web server | `iis-site` | `command: [systemctl, ...]` |
| Bridge | `com-register` / `com-unregister` | none (no native bridge) |
| Database | `sql` against SQL Server | `sql` against PostgreSQL |
| Package | zip (`package` step) | tarball (`package` step) |
| Healthcheck | `http://.../health` | `tcp://127.0.0.1:7700` |
| Reversal | `rev:bridge.register.x86`, `rev:iis.site.create` | `rev:svc.stop` |
| Steps total | 10 | 11 |
| Forward runnable | 8 (2 are rev:) | 10 (1 is rev:) |

## What the engine treats the same

For both descriptors:

  * The same loader parses the YAML.
  * The same planner produces a DAG and auto-wires `rev:` pairs.
  * The same executor dispatches steps in topological order, retries
    on failure, skips downstream steps on upstream failure, and
    runs the corresponding reversal on a forward failure.
  * The same MockTransport records every action and supports
    chaos injection through `--chaos '{"host":[…]}'`.
  * The same CLI (`orchx plan`, `orchx deploy`) drives everything.

## What is *not* shared

  * The transport implementation. For real hosts, the OAuth
    descriptor would target a `ssh://` or `winrm://` URI; the engine
    is oblivious to which.
  * The actual on-host commands. The IIS site creation step runs
    `New-Item IIS:\Sites\…`; the systemd step runs
    `systemctl start …`. Both are delivered to the host by a
    transport; the engine does not care which shell they live in.

## Running them

```bash
# Validate both descriptors
orchx plan descriptors/sample_webapp_erp.yaml
orchx plan descriptors/sample_oauth_service.yaml

# Deploy both against the mock (no real host touched)
orchx deploy descriptors/sample_webapp_erp.yaml      --target mock://local
orchx deploy descriptors/sample_oauth_service.yaml   --target mock://local

# Failure path on either descriptor
orchx deploy descriptors/sample_oauth_service.yaml   --target mock://local \
    --chaos '{"local":[{"action":"command","exit_code":1,"fail_times":99}]}'
```

The OAuth descriptor's failure injects a `command` action failure.
The webapp-ERP descriptor's typical failure is `package` or
`iis-site` — see `tests/test_executor.py` for examples.

## Adding a third topology

The pattern is straightforward:

  1. Copy one of the sample descriptors.
  2. Rename `system.code` (must be lowercase letters / digits / `_`).
  3. Replace the `steps:` with what your stack actually needs.
  4. If you need a step type we don't ship yet, add it to
     `src/orchx/descriptor/models.py`, an adapter in
     `src/orchx/steps/steps.py`, and a method in `Transport`.
  5. Add a test in `tests/test_descriptors.py` if you want
     CI to lock the topology's behaviour down.

If a step's *semantics* are unique to your platform (e.g. systemd
`daemon-reload`, `firewall-cmd`, `iptables`), prefer the generic
`command` step with a clear `cmd:` rather than inventing a new
step type. Step types are for things that are common enough to
warrant a typed surface (IIS sites, COM bridges, SQL execution);
one-off shell calls belong in `command` / `powershell`.
