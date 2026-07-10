# Roadmap

This document is the public roadmap for OrchX. It lists what
we are **not** doing in v0.x, what we are **planning** for
v0.3, and a few options we are **considering** for v0.4+.

Scope changes are signalled by the version number:

  * **0.1.x**: the "ship it" line. Polish, bugfixes, and small
    additive descriptors. **Frozen at v0.1.7.**
  * **0.2.x** (current): real-world integrations and reliability
    work the v0.1.x line has not had time for.
  * **0.3.x** (planned, scoped by customer demand): the larger
    lifts — auth, real vault backends, transport cancel.

## v0.2 — "operate it in production" ✅ shipped

The v0.2 line is now closed. It shipped in three tag-anchored
slices:

### v0.2.0-alpha — pagination + concurrent write safety

- `GET /api/runs?limit=&offset=&state_filter=` with a
  `total` field in the response. limit is capped at 500;
  offset is normalised to `>= 0`.
- Three new indexes on `runs`: `created_at DESC`, `state`,
  and the composite `(state, created_at DESC)`.
- WAL journal mode + `busy_timeout=5000` + `synchronous=NORMAL`.
- One `asyncio.Lock` per `RunStore` serialises writes
  (`create_run`, `update_run`, `emit`); reads remain concurrent
  under WAL.
- Tests: 5 pagination tests + 1 concurrent-write stress test
  (8 simultaneous POSTs from a thread pool).

### v0.2.0-beta — dashboard pagination + state filter UI

- A `State:` filter dropdown on the runs panel (all / pending
  / running / ok / failed / aborted) that maps to
  `?state_filter=`.
- A `Page X of Y · N total` footer with `Prev` / `Next`
  buttons. PAGE_SIZE is 25.
- `onFilterChange()` resets the page cursor to 0 so changing
  the filter doesn't leave the operator on a stale page.

### v0.2.0 — operational CLI + OpenAPI

- New flags on `orchx deploy`:
    * `--verbose` / `-v` — keeps the rich UI on stdout, and
      writes one JSON event per line to stderr.
    * `--json` — suppresses the rich UI; emits a single
      `RunReport` JSON on stdout. The CLI exit code matches
      the report's `exit_code`, so shell pipelines and CI
      jobs can detect failures cleanly.
- New helpers in the CLI module:
    * `_make_json_event_emitter` for `--json` / `--verbose`.
    * `_report_to_dict` for the JSON serialisation.
- FastAPI now exposes OpenAPI:
    * `/api/openapi.json`
    * `/api/docs` (Swagger UI)
    * `/api/redoc` (ReDoc)
- Version bumped to 0.2.0 across `pyproject.toml`,
  `src/orchx/__init__.py`, the FastAPI app, and the README
  badge.
- Tests: 4 new CLI subprocess tests + 1 new OpenAPI test.
- **Total: 92 tests passing.**

## v0.3 — "operate it at scale" (planned)

Scoping depends on real customer demand. Three candidate
themes, **ordered by current commercial priority**:

### In scope (v0.3)

1. **Auth on the dashboard.**
   The dashboard today is unauthenticated. Any commercial
   deployment needs at minimum a basic-auth / API-key
   guard, and ideally a pluggable authn backend. We will
   ship:
     * `ORCHX_AUTH_MODE=basic|api_key|none` (env var).
     * A single env-var-based credential store at first;
       pluggable later (HashiCorp Vault, LDAP, OIDC).
     * `Authorization: Bearer <key>` on the JSON API and
       the WebSocket; same key for the dashboard login.
2. **Real vault backends.**
   The `secrets` module today supports `env`, `file`, and
   `memory`. The v0.3 line adds:
     * **HashiCorp Vault** (`vault://server:8200/...`) via
       the official HTTP API.
     * **AWS Secrets Manager** (`aws://region/secret-name`).
     * **Kubernetes-native** (`k8s://namespace/secret-name`).
   The descriptor-side `{{ secret.x }}` syntax stays exactly
   the same; only the backend changes.
3. **Transport cancel signal.**
   `should_cancel` is checked between steps but the in-flight
   transport call (e.g. asyncssh `conn.run()`) is not
   interrupted. v0.3 adds:
     * `Transport.cancel()` on the protocol, with a default
       no-op for transports that can't interrupt mid-call.
     * asyncssh / pywinrm implementations that close the
       underlying channel on cancel.
     * The executor awaits the cancel before starting the
       next step, so a cancelled run never lingers in
       `running` state.

### Out of scope for v0.3 (punted to v0.4+)

- **Multi-host runs.** The DAG is single-host today. Multi-host
  needs a global state machine that survives any single host's
  failure, and a new descriptor shape. v0.4 at the earliest.
- **A new transport** (Docker exec, Ansible, k8s, …). The
  transport protocol is stable; the next transport comes when
  a customer needs it.
- **A new step kind** (terraform plan, k8s apply, …). Same story.

## v0.4+ — "orchestrate a fleet" (considering)

Three candidate themes:

1. **Multi-host.** A run spans N machines with N distinct
   descriptors. The DAG becomes a tree instead of a list. We
   need a global state machine that survives any single
   host's failure.
2. **Secret-bound rollout.** Every step that touches a host
   is annotated with the secret it needs, and the run refuses
   to start unless every secret resolves. The audit log
   becomes a compliance artefact.
3. **SLA + quotas.** A run can specify a deadline; if a
   step is going to miss it, the executor pages someone.
   Quotas per host per minute to keep the dashboard from
   accidentally DDoS-ing a database.

These three are **not** mutually exclusive. We will pick the
one that solves a paying customer's problem first.

## What we are explicitly not doing

- **A "do-everything" UI.** The dashboard is intentionally a
  thin view over the run history. Anyone serious about
  operations will want a real control plane (Argo, Spinnaker,
  their own thing) on top, and OrchX is the engine underneath.
- **Generic Terraform-style plan/apply.** "Plan" is already a
  first-class verb (`orchx plan <descriptor>`); "apply" is
  `orchx deploy`. The runner is a transport, not a language.
- **Multi-tenant SaaS of OrchX itself.** Each customer runs
  their own instance. The control plane is a process, not a
  hosted product. (We'll know when this changes.)

## How to influence the roadmap

- File an issue on the repo with the `roadmap` label. The
  `use-case:` prefix on the issue title is the most useful:
  "use-case: 50-host web farm rolling upgrade", etc.
- For paying customers we adjust the order, not the goals.