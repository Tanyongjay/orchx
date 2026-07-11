# Roadmap

This document is the public roadmap for OrchX. It lists what
we are **not** doing in v0.x, what we are **planning** for
v0.4, and a few options we are **considering** for v0.5+.

Scope changes are signalled by the version number:

  * **0.1.x**: the "ship it" line. Polish, bugfixes, and small
    additive descriptors. **Frozen at v0.1.7.**
  * **0.2.x** (closed): real-world integrations and reliability
    work — pagination, concurrent writes, structured CLI,
    OpenAPI. **Final v0.2.1.**
  * **0.3.x** (closed): operational hardening. Auth gate on
    the control plane, dashboard login flow, real-SSH
    end-to-end verification. **Final v0.3.0.**
  * **0.4.x** (planned): real vault backends + transport cancel
    + operational polish.
  * **0.5.x+** (considering): multi-host, secret-bound rollouts,
    SLA + quotas.

## v0.3 — "operate it at scale" ✅ shipped

The v0.3 line is now closed. It shipped in three slices
plus the final tag, each solving a production-readiness
gap that came up the first time we pointed orchx at a
real host.

### v0.3.0-alpha — authentication gate

- 3 modes (`none` / `basic` / `api_key`) configured via
  `ORCHX_AUTH_MODE` plus the per-mode variables.
- Middleware-based gate on every `/api/*` path. /healthz
  and /api/auth stay open.
- WebSocket credential check on the upgrade request
  (browsers can't set custom headers, so the credential
  travels as `?token=` for api_key or `?basic=` for basic).
- OpenAPI / Swagger UI exempt so the API contract is
  browsable without a credential.

### v0.3.0-beta — dashboard login flow

- A login modal in the dashboard asks for the credential
  on first load when the server reports auth required.
- The credential is stored in `localStorage` and re-
  attached to every fetch() call by a window-level wrapper.
- The WebSocket URL builder encodes the credential as a
  query parameter (see alpha).
- A "Sign out" link clears `localStorage` and reloads.

### v0.3.0 — real-SSH end-to-end

- A new sample descriptor `sample_ssh_smoke.yaml` exercises
  the SSH transport with the smallest possible surface
  (4 command steps, no systemd, no PostgreSQL, no venv).
- `examples/run_real_ssh.py` is a paramiko-based smoke
  driver that runs on Windows hosts without needing
  `sshpass`.
- The settle-eod descriptor's sudo-requiring commands
  now wrap `sudo -n` so `jay-with-sudo` (the typical
  production account) can run the full deploy without
  being root.
- Verified end-to-end against 192.168.10.241 (Ubuntu
  24.04, jay user, password auth). All 4 steps exit=0.

## v0.4 — "production vault + cancellation" (planned)

The v0.4 line adds the two operational primitives that
make orchx safe to leave running unattended: a real
secret backend, and a way to actually stop a runaway run.

### In scope (v0.4)

1. **Real vault backends.** Today secrets are read from
   `env`, `file`, or `memory`. The v0.4 line adds:
     * `vault://` — HashiCorp Vault via the official
       HTTP/2 + KV-v2 API. The descriptor's `{{ secret.x }}`
       syntax is unchanged; only the backend changes.
     * `aws://` — AWS Secrets Manager via boto3.
     * `k8s://` — Kubernetes-native Secret access via the
       in-cluster service account.
   Pluggable via `ORCHX_SECRETS_BACKEND`. Each backend
   is one file under `src/orchx/secrets/`; the existing
   backends stay so v0.2.x deployments keep working.
2. **Transport cancel signal.** The `should_cancel` hook
   is checked between steps but the in-flight transport
   call (e.g. asyncssh `conn.run()`) is not interrupted.
   v0.4 adds:
     * `Transport.cancel()` on the protocol, with a default
       no-op for transports that can't interrupt mid-call.
     * `asyncssh` / `pywinrm` implementations that close
       the underlying channel on cancel.
     * The executor awaits the cancel before starting the
       next step, so a cancelled run never lingers in
       `running` state.
3. **Operational polish.**
     * Dashboard run detail page links to the descriptor
       file on the host (so an operator can diff the YAML
       that produced a run).
     * `orchx doctor` — a one-shot command that runs the
       full connectivity check (target reachability,
       secrets resolution, descriptor load, plan DAG)
       and prints a single report.
     * Lock-down test for cancel: a chaos descriptor
       that injects a 60s sleep into one step, with the
       executor requested to cancel after 5s. Asserts
       the transport call is interrupted, not just the
       surrounding event loop.

### Out of scope for v0.4 (punted to v0.5+)

- **Multi-host runs.** The DAG is single-host today.
  Multi-host needs a global state machine that survives
  any single host's failure, and a new descriptor shape.
  v0.5 at the earliest.
- **A new transport** (Docker exec, Ansible, k8s, …). The
  transport protocol is stable; the next transport comes
  when a customer needs it.
- **A new step kind** (terraform plan, k8s apply, …).
  Same story.

## v0.5+ — "orchestrate a fleet" (considering)

Three candidate themes:

1. **Multi-host.** A run spans N machines with N distinct
   descriptors. The DAG becomes a tree instead of a list.
2. **Secret-bound rollout.** Every step that touches a
   host is annotated with the secret it needs, and the
   run refuses to start unless every secret resolves.
3. **SLA + quotas.** A run can specify a deadline; if a
   step is going to miss it, the executor pages someone.

These three are **not** mutually exclusive. We will pick
the one that solves a paying customer's problem first.

## What we are explicitly not doing

- **A "do-everything" UI.** The dashboard is intentionally
  a thin view over the run history. Anyone serious about
  operations will want a real control plane (Argo, Spinnaker,
  their own thing) on top, and OrchX is the engine underneath.
- **Generic Terraform-style plan/apply.** "Plan" is
  already a first-class verb (`orchx plan <descriptor>`);
  "apply" is `orchx deploy`. The runner is a transport,
  not a language.
- **Multi-tenant SaaS of OrchX itself.** Each customer
  runs their own instance. The control plane is a process,
  not a hosted product. (We'll know when this changes.)

## How to influence the roadmap

- File an issue on the repo with the `roadmap` label. The
  `use-case:` prefix on the issue title is the most useful:
  "use-case: 50-host web farm rolling upgrade", etc.
- For paying customers we adjust the order, not the goals.
