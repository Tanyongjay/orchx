# Roadmap

This document is the public roadmap for OrchX. It lists what
we are **not** doing in v0.x, what we are **planning** for v0.2,
and a few options we are **considering** for v0.3+.

Scope changes are signalled by the version number:

  * **0.1.x** (current): the "ship it" line. Polish, bugfixes,
    and small additive descriptors.
  * **0.2.x** (next): real-world integrations and reliability
    work the v0.1.x line has not had time for.
  * **0.3.x** (planned, scoped by customer demand): the
    larger lifts — multi-host, secrets, the next transport.

## v0.2 — "operate it in production"

Target window: once we have at least one customer running on
real hosts (ssh or winrm) for a few weeks and we know what
breaks in real conditions.

### In scope

- **Real-host runs.** Support ssh and winrm targets end-to-end.
  This includes real error reporting (no more "ok" for
  `tcp_open` against a host that doesn't exist), real timing
  metrics, and a documented "blue/green" pattern for zero-downtime
  cutover.
- **Secrets in descriptors.** The descriptor schema is already
  wired for `{{ secret.x }}` substitution; the missing piece is
  the CLI flag to point at a real vault, the audit log of which
  run resolved which secret, and the secret-rotation story.
- **Structured run reports.** JSON artefacts on disk per run
  (run.json + events.jsonl + plan.json) so a CI pipeline can
  grep them, fail the build, and link to the artefact.
- **Step-level retries with backoff that respects the cancel
  signal.** Right now retry loops ignore cancel; this is fine
  for the mock but wrong for the real world.
- **Concurrent run limit + queue.** Cap how many runs can be
  in-flight per host so a flooded dashboard doesn't spawn 200
  psql sessions.
- **Better error messages.** Move all `CommandResult(stderr=...)`
  into the run event log so the user can see *why* a step
  failed without digging through host logs.

### Out of scope for v0.2

- A new transport (Docker exec, Ansible, k8s, …). The transport
  protocol is stable; the next transport comes when a customer
  actually needs it.
- A new step kind (terraform plan, k8s apply, …). Same story.
- Multi-host, multi-region runs. The DAG is single-host; we
  have no notion of cross-host orchestration. v0.3 at the
  earliest.

## v0.3 — "orchestrate a fleet" (planned)

Scoping depends on real customer demand. Three candidate
themes:

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
