# OrchX

> **Multi-system deploy orchestrator.** Describe a stack in YAML, run it
> against any target, watch it on a live dashboard. Vendor-neutral by policy.

[![ci](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml/badge.svg)](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml)
[![Vendor gate](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml/badge.svg?job=name-gate)](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml?query=job%3Aname-gate)
[![ruff](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml/badge.svg?job=lint)](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml?query=job%3Alint)
[![pytest](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml/badge.svg?job=tests)](https://github.com/Tanyongjay/orchx/actions/workflows/ci.yml?query=job%3Atests)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version: v0.1.6](https://img.shields.io/badge/version-v0.1.6-blue.svg)](https://github.com/Tanyongjay/orchx/releases)

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
- **🔐 Secret-aware UI** — events whose step touches the orchx vault
  show a 🔐 indicator on the timeline. Secrets are resolved only at
  step-execute time and never land in the descriptor model, the SQLite
  store, the event stream, or test fixtures
- **5 sample descriptors** — Windows/IIS, Linux/systemd, Linux/docker
  compose, Linux/Python venv/supervisor, Linux/cron. Same engine
  drives all of them
- **81 tests**, ruff clean, vendor-name CI gate, GitHub Actions

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

## Five sample descriptors, five stacks

| | `sample_webapp_erp.yaml` | `sample_oauth_service.yaml` | `sample_containerized_saas.yaml` | `sample_hr_service.yaml` | `sample_settle_eod.yaml` |
|---|---|---|---|---|---|
| Host | Windows (IIS, COM, SQL) | Linux (systemd, PostgreSQL) | Linux (docker compose) | Linux (venv, uwsgi, supervisor) | Linux (cron, Python venv) |
| Service | IIS site | systemd unit | 3 containers (app, worker, db) | uwsgi under supervisor | **no service** — cron job |
| Bridge | COM | — | — | — | — |
| Database | SQL Server | PostgreSQL | PostgreSQL | PostgreSQL | PostgreSQL (ledger) |
| Healthcheck | `http://…/health` | `tcp://…:7700` | 3× `tcp://` | `http://…/healthz` | wheel-import smoke |
| Uses secrets | no | no | no | **yes** | **yes** |
| Steps | 10 | 11 | 15 | 11 | 10 |

The same engine / executor / CLI / dashboard drive all five. See
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
| v0.1  | FastAPI control plane, dashboard, secrets, 🔐 indicator | ✅ shipped |
| v0.2  | Operational hardening (see [`docs/ROADMAP.md`](docs/ROADMAP.md)) | 🟡 in progress |

See [`docs/CI.md`](docs/CI.md) for the current green build.
