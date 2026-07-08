# OrchX

> **Multi-system deploy orchestrator.** Describe a stack in YAML, run it
> against any target, watch it on a live dashboard. Vendor-neutral by policy.

OrchX is a generic orchestrator that describes, plans, and executes the
deployment of arbitrary enterprise systems across heterogeneous hosts.
It is built around a single YAML descriptor per system and a pluggable
transport (mock / WinRM / SSH), so the same engine deploys many kinds
of stacks without code changes.

The engine has no opinions about your stack: it consumes a YAML DAG,
dispatches step kinds through a `Transport` protocol, and tracks state
with explicit rollback semantics. A FastAPI control plane ships in
the box.

## Highlights

- **YAML descriptors** — one file per system; no Python to write
- **12 step kinds** — check, powershell, command, com-register, iis-site,
  sql, package, http, healthcheck (http + tcp), plus reversals via the
  `rev:<id>` convention
- **3 transports** — `mock://` (in-process), `winrm://` (HTTPS/5986),
  `ssh://` (libssh-backed); real transports are opt-in via
  `uv sync --extra real`
- **DAG + state machine** — retries, skip-on-failure, automatic rollback
  of every OK step on forward failure
- **FastAPI control plane** — REST + WebSocket + SQLite for run history
- **Live dashboard** — pick a descriptor, hit Deploy, watch the timeline
- **66 tests**, ruff clean, vendor-name CI gate, GitHub Actions

## Quick start

```bash
# 1. Install
uv sync --extra dev        # base + dev tools (mock transport only)
uv sync --extra real       # add pywinrm + asyncssh for real targets

# 2. CLI: plan + deploy against the mock
orchx plan  descriptors/sample_webapp_erp.yaml
orchx deploy descriptors/sample_webapp_erp.yaml --target mock://local

# 3. Web control plane (live dashboard)
uv run python -m orchx.web.app
# open http://localhost:8000/ in a browser
```

## Two sample descriptors, two stacks

| | `sample_webapp_erp.yaml` | `sample_oauth_service.yaml` |
|---|---|---|
| Host | Windows (IIS, COM, SQL Server) | Linux (systemd, PostgreSQL) |
| Web | `iis-site` step | `command: [systemctl, ...]` |
| Database | SQL Server (T-SQL) | PostgreSQL (DO block) |
| Package | zip | tarball |
| Healthcheck | `http://.../health` | `tcp://127.0.0.1:7700` |
| Steps | 10 | 11 |

The same engine / executor / CLI / dashboard drive both. See
[`docs/SAMPLE_TOPOLOGIES.md`](docs/SAMPLE_TOPOLOGIES.md) for the
side-by-side.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│        Web dashboard (vanilla JS, single page)                │
│        + JSON API + WebSocket events                          │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│              FastAPI control plane (orchx.web)                │
│  REST  /api/runs, /api/runs/{id}, /api/runs/{id}/cancel      │
│  WS    /api/runs/{id}/stream                                  │
│  SQLite-backed run history (survives restarts)                │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                 Engine (orchx.engine)                         │
│  * Descriptor loader (YAML → Pydantic)                       │
│  * Planner (DAG + topo sort + cycle check)                    │
│  * Executor (state machine: pending → running → ok/failed)   │
│  * Rollback (auto-reverses OK steps on forward failure)      │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌────────────┬─────────────┼─────────────┬─────────────────────┐
│ mock://    │ winrm://    │ ssh://      │  (more, as needed)  │
│ (in-mem)   │ (pywinrm)   │ (asyncssh)  │                     │
└────────────┴─────────────┴─────────────┴─────────────────────┘
```

## Layout

```
src/orchx/
  cli/             Typer CLI: plan / deploy / validate
  descriptor/      Pydantic + YAML schema + template rendering
  engine/          DAG planner, state machine, executor
  steps/           Built-in step adapters + forward/reverse runner
  transports/      Transport protocol + mock + winrm + ssh
  secrets/         Backend pluggable: env / memory / file
  utils/           logging, retries
  web/             FastAPI app + SQLite store + dashboard HTML
descriptors/       Sample YAML descriptors (vendor-neutral)
tests/             Pytest (DAG + executor + web + transports + secrets)
docs/              Architecture, naming, sample topologies, CI
scripts/           Vendor-name CI gate
.github/workflows/ CI pipeline
```

## Vendor neutrality

This project is **vendor-neutral by policy**. The CI gate
(`scripts/check_vendor_names.py`) rejects commits that introduce
any vendor name (product, SKU, or directory path) into the source
tree, descriptors, tests, logs, or docs. See
[`docs/NAMING_GUIDELINES.md`](docs/NAMING_GUIDELINES.md) for the
guarded name list and the rationale.

## Contributing

1. Read [`docs/NAMING_GUIDELINES.md`](docs/NAMING_GUIDELINES.md)
2. `uv sync --extra dev`
3. Add tests for any new step kind or transport behaviour
4. `pytest` + `ruff check` + `python scripts/check_vendor_names.py`
5. Open a PR

## Status

| Stage | Scope | Status |
|---|---|---|
| MVP-1 | Engine + Mock transport + sample descriptor | ✅ shipped |
| MVP-2 | WinRM + SSH transports, retries, rollback | ✅ shipped |
| v0.1  | FastAPI control plane, dashboard, secrets | ✅ shipped |

See [`docs/CI.md`](docs/CI.md) for the current green build.
