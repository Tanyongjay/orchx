# OrchX

**Multi-system deploy orchestrator for Windows + SQL + native bridge topologies.**

OrchX is a generic, vendor-neutral orchestrator that describes, plans, and executes the
deployment of arbitrary enterprise systems across Windows machines. It is built around
a single YAML descriptor per system and a pluggable transport (mock / WinRM / SSH),
so the same engine can deploy many kinds of stacks without code changes.

## Why

Traditional enterprise deploys involve a long list of vendor-specific steps
(web server role, native COM bridge registration, SQL database creation,
package/upgrade runners, IIS parent paths, file ACLs, license handlers…).
OrchX turns that checklist into a versioned, idempotent, inspectable plan that
is shared across engineering, ops, and CI.

## Status

| Stage | Scope | Status |
|---|---|---|
| MVP-1 | Engine + Mock transport + one sample descriptor | in progress |
| MVP-2 | WinRM transport, retries, healthchecks, rollback | planned |
| v1     | FastAPI control plane, audit log, secrets vault | planned |

This repository contains the MVP-1 skeleton. Real transports (`winrm`, `ssh`) are
opt-in via `pip install orchx[real]` and intentionally not in the default extras
to keep the dev loop clean.

## Install (dev)

```bash
uv sync                          # base MVP-1 (mock only)
uv sync --extra real             # add WinRM + SSH transports
uv sync --extra dev              # add test + lint tools
```

## Quick start

```bash
# Validate the bundled sample descriptor
orchx plan descriptors/sample_webapp_erp.yaml

# Dry-run (renders the execution graph, prints actions, does not touch any host)
orchx deploy descriptors/sample_webapp_erp.yaml --target mock://local --dry-run

# Actually run (against the mock transport; no real host is touched)
orchx deploy descriptors/sample_webapp_erp.yaml --target mock://local
```

## Repo layout

```
src/orchx/         core package
  cli/             Typer CLI entrypoint
  descriptor/      Pydantic + YAML descriptor schemas
  engine/          DAG planner, state machine, executor
  steps/           Built-in step types (check, powershell, iis-site, sql, ...)
  transports/      Transport abstraction + Mock + WinRM placeholder
  utils/           logging, retries, secrets
descriptors/       Sample YAML descriptors (one per known topology)
tests/             Pytest smoke + DAG + state machine tests
docs/              Architecture, naming guidelines, adding new step types
```

## Rules we follow

This project is **vendor-neutral by policy**. See [`docs/NAMING_GUIDELINES.md`](docs/NAMING_GUIDELINES.md)
for the full list of guarded names and `scripts/check_vendor_names.py` for the
CI gate. Please read it before writing any new module, descriptor, test, log,
or config key.
