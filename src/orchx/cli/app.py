"""OrchX CLI entrypoint.

Commands:
  * ``orchx plan <descriptor>``   — parse, validate, render DAG, no I/O.
  * ``orchx deploy <descriptor>`` — render DAG and execute against a transport.
  * ``orchx validate <descriptor>`` — strict YAML/validation only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from orchx.cli.render import render_plan_table
from orchx.descriptor.loader import load_descriptor, render_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.utils.logging import make_logger

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()
log = make_logger(console)


@app.command()
def validate(
    descriptor: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Validate a descriptor without executing it."""
    try:
        load_descriptor(descriptor)
    except Exception as e:
        log.error(str(e))
        raise typer.Exit(code=2) from None
    log.info(f"OK {descriptor}")


@app.command()
def doctor(
    descriptor: Path = typer.Argument(..., exists=True, readable=True),
    target: str = typer.Option("mock://local", "--target", "-t"),
    secrets_backend: str | None = typer.Option(
        None,
        "--secrets-backend",
        help=("Backend used to resolve {% secret x %} tokens. env|file|memory (default: env)."),
    ),
    secrets_file: Path | None = typer.Option(
        None,
        "--secrets-file",
        help="Path to a secrets file (when --secrets-backend=file).",
    ),
) -> None:
    """Run preflight checks against a target and descriptor.

    doctor prints one PASS/FAIL line per check and exits
    non-zero if any check fails. It does NOT deploy; it
    only verifies that the deploy WILL work if you run
    it. Use this after editing a descriptor or moving
    to a new target host.

    The checks are:
      1. Descriptor load: parse the YAML, validate the
         Pydantic model, render templates.
      2. Plan DAG: build the dependency graph and check
         for cycles and missing rev: pairs.
      3. Secret resolution: for every {{ secret.x }} token
         in the descriptor, look it up in the active vault
         backend and report missing names.
      4. Target reachability: TCP connect to host:port
         so the operator sees a fast FAIL if a firewall
         is blocking the connection.
      5. Auth: for ssh, the key file (if any) exists and
         is readable; for winrm, the URI carries credentials.
    """
    from urllib.parse import parse_qs, urlparse

    from orchx.descriptor.loader import (
        _ctx_from_descriptor,
        load_descriptor,
        render_descriptor,
    )
    from orchx.engine.planner import build_plan
    from orchx.secrets import get_vault

    failures = 0

    def _check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        marker = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        line = f"  {marker}  {label}"
        if detail:
            line += f"   ({detail})"
        console.print(line)
        if not ok:
            failures += 1

    # 1. Descriptor load.
    try:
        parsed = load_descriptor(descriptor)
        _check(
            "descriptor load",
            True,
            f"{descriptor} ({len(parsed.steps)} steps)",
        )
    except Exception as e:
        _check("descriptor load", False, str(e))
        console.print()
        console.print(f"[red]doctor: {failures} check(s) failed[/red]")
        raise typer.Exit(code=1) from None

    # 2. Plan DAG.
    try:
        plan = build_plan(parsed)
        n_steps = len(plan.nodes)
        n_revs = sum(1 for sid in plan.nodes if sid.startswith("rev:"))
        _check(
            "plan DAG",
            True,
            f"{n_steps} nodes ({n_revs} rev: pairs)",
        )
    except Exception as e:
        _check("plan DAG", False, str(e))

    # 3. Secret resolution.
    try:
        vault_kwargs: dict[str, object] = {}
        if secrets_file is not None:
            vault_kwargs["path"] = secrets_file
        vault = get_vault(secrets_backend, **vault_kwargs)

        # render_template intentionally does NOT consult the
        # vault; it leaves {{ secret.x }} tokens untouched.
        # But it does call ctx["_secret_probe"].add(name) for
        # every name it sees, so the doctor can preflight the
        # vault without ever resolving a value. See the
        # security note in render_template's docstring for
        # why this is the safe way to do it.
        seen: set[str] = set()
        ctx = dict(_ctx_from_descriptor(parsed))
        ctx["_secret_probe"] = seen
        with contextlib.suppress(Exception):
            # If render fails, the doctor still continues
            # to the next check; the user gets the real
            # failure when they actually deploy.
            render_descriptor(parsed, ctx)
        missing: list[str] = []
        for name in sorted(seen):
            try:
                vault.resolve(name)
            except Exception:
                missing.append(name)
        if missing:
            _check(
                "secrets",
                False,
                f"missing in vault: {', '.join(missing)}",
            )
        elif not seen:
            _check("secrets", True, "no secret tokens in descriptor")
        else:
            n = len(seen)
            _check("secrets", True, f"{n} name(s) resolved")
    except Exception as e:
        _check("secrets", False, str(e))

    # 4. Target reachability.
    try:
        parsed_url = urlparse(target)
        scheme = parsed_url.scheme.lower()
        if scheme in ("mock",):
            _check("target reachability", True, "mock:// (in-process)")
        else:
            import socket

            host = parsed_url.hostname or ""
            port = parsed_url.port or 22
            s = socket.socket()
            s.settimeout(5)
            try:
                s.connect((host, port))
                _check(
                    "target reachability",
                    True,
                    f"{host}:{port} ({scheme})",
                )
            except OSError as e:
                _check("target reachability", False, f"{host}:{port} {e}")
            finally:
                s.close()
    except Exception as e:
        _check("target reachability", False, str(e))

    # 5. Auth.
    try:
        parsed_url = urlparse(target)
        scheme = parsed_url.scheme.lower()
        if scheme == "ssh":
            qs = parse_qs(parsed_url.query)
            key = qs.get("key", [None])[0]
            if key:
                from pathlib import Path as _P

                p = _P(key)
                if p.exists() and p.is_file():
                    _check("auth", True, f"ssh key {key}")
                else:
                    _check("auth", False, f"ssh key not found: {key}")
            else:
                _check(
                    "auth",
                    True,
                    "ssh (no key in URI; password or agent auth assumed)",
                )
        elif scheme in ("winrm", "winrm-http"):
            if parsed_url.username and parsed_url.password:
                _check("auth", True, f"{scheme} (creds in URI)")
            else:
                _check("auth", False, f"{scheme} URI missing user/password")
        elif scheme == "mock":
            # Already handled in target reachability.
            pass
        else:
            _check("auth", False, f"unknown scheme: {scheme}")
    except Exception as e:
        _check("auth", False, str(e))

    console.print()
    if failures:
        console.print(f"[red]doctor: {failures} check(s) failed[/red]")
        raise typer.Exit(code=1) from None
    console.print("[green]doctor: all checks passed[/green]")


@app.command()
def plan(
    descriptor: Path = typer.Argument(..., exists=True, readable=True),
    target: str = typer.Option(
        "mock://local",
        "--target",
        "-t",
        help="Transport URI. Prefix scheme (mock|winrm|ssh) selects transport.",
    ),
    set_: list[str] = typer.Option(
        [],
        "--set",
        "-s",
        help="Override descriptor variables, e.g. --set system.version=1.0.0",
    ),
) -> None:
    """Render the execution DAG."""
    parsed = load_descriptor(descriptor)
    overrides = _parse_overrides(set_)
    if overrides:
        parsed = render_descriptor(parsed, _apply_overrides_to_ctx(overrides))

    plan = build_plan(parsed)
    console.print(
        Panel.fit(
            f"[bold]system[/bold] {parsed.system.name} v{parsed.system.version}\n"
            f"roles: {', '.join(r.name for r in parsed.topology.roles)}",
            title="OrchX plan",
        )
    )
    console.print(render_plan_table(parsed, plan))

    if not plan.all_done():
        console.print("[yellow]plan: not yet executed; use 'orchx deploy' to run.[/yellow]")


@app.command()
def deploy(
    descriptor: Path = typer.Argument(..., exists=True, readable=True),
    target: str = typer.Option("mock://local", "--target", "-t"),
    set_: list[str] = typer.Option([], "--set", "-s"),
    secrets_backend: str | None = typer.Option(
        None,
        "--secrets-backend",
        help=(
            'Backend used to resolve {% secret "name" %} tokens in the '
            "descriptor and the target URI. env|file|memory (default: env)."
        ),
    ),
    secrets_file: Path | None = typer.Option(
        None,
        "--secrets-file",
        help="Path to a secrets file (when --secrets-backend=file).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render only, no I/O."),
    rollback: bool = typer.Option(
        True,
        "--rollback/--no-rollback",
        help="Reverse previously-OK steps on failure.",
    ),
    chaos: str | None = typer.Option(
        None,
        "--chaos",
        help=(
            "Inject mock-transport failure rules as JSON, e.g. "
            '\'{"host1":[{"action":"iis-site","exit_code":1,"fail_times":1}]}\''
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print structured log lines (one JSON per event) to stderr.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit a single JSON RunReport to stdout and skip the rich UI.",
    ),
) -> None:
    """Deploy against the chosen transport."""
    from orchx.secrets import get_vault, substitute_secrets

    vault_kwargs: dict[str, object] = {}
    if secrets_file is not None:
        vault_kwargs["path"] = secrets_file
    vault = get_vault(secrets_backend, **vault_kwargs)

    # Resolve secrets inside the target URI first, so the resolved
    # form never ends up in process listings / argv.
    target = substitute_secrets(target, vault)

    parsed = load_descriptor(descriptor, vault=vault)
    overrides = _parse_overrides(set_)
    if overrides:
        parsed = render_descriptor(parsed, _apply_overrides_to_ctx(overrides))

    plan_obj = build_plan(parsed)

    if dry_run:
        console.print(Panel.fit("DRY RUN — no host interaction", style="cyan"))
        console.print(render_plan_table(parsed, plan_obj))
        raise typer.Exit(code=0)

    if chaos:
        os.environ["ORCHX_MOCK_CHAOS"] = chaos

    from orchx.transports import get_transport

    transport = get_transport(target)

    if json_output:
        # The JSON path is for scripting: suppress rich, capture
        # structured events, and emit a single report at the end.
        on_event = _make_json_event_emitter()
    else:
        on_event = _make_event_emitter(console, verbose=verbose)

    exec_ = Executor(
        descriptor=parsed,
        plan=plan_obj,
        transport=transport,
        rollback_on_failure=rollback,
        on_event=on_event,
    )
    import asyncio

    report = asyncio.run(exec_.run())
    if json_output:
        # Flush any pending structured log lines before the final
        # report so callers see them in order.
        sys.stderr.flush()
        print(json.dumps(_report_to_dict(report), indent=2, sort_keys=True))
    else:
        console.print()
        console.print(_summary(report))
    raise typer.Exit(code=report.exit_code)


# ---- helpers ----


def _parse_overrides(items: list[str]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"--set expects key=value, got: {item!r}")
        k, v = item.split("=", 1)
        if "." not in k:
            raise typer.BadParameter(f"--set expects dotted key, got: {k!r}")
        ctx, key = k.split(".", 1)
        out[(ctx, key)] = v
    return out


def _apply_overrides_to_ctx(ov: dict[tuple[str, str], str]) -> dict[str, dict[str, str]]:
    """Map `--set system.version=...` -> ctx override dict."""
    out: dict[str, dict[str, str]] = {}
    for (ctx, key), value in ov.items():
        out.setdefault(ctx, {})[key] = value
    return out


def _make_event_emitter(console_: Console, *, verbose: bool = False):
    from orchx.engine.models import StepAttempt

    def emit(node, attempt: StepAttempt) -> None:
        sym = {
            "ok": "[green]\u2713[/green]",
            "failed": "[red]\u2717[/red]",
            "skipped": "[yellow]~[/yellow]",
            "rolling_back": "[magenta]\u27f2[/magenta]",
            "rolled_back": "[green]\u2713 rb[/green]",
            "rollback_failed": "[red]\u2717 rb[/red]",
        }
        s = sym.get(attempt.status.value, f"[blue]{attempt.status.value}[/blue]")
        msg = f" {attempt.message}" if attempt.message else ""
        console_.print(
            f"  {s}  {node.step_id:24s}  try={attempt.attempt}  host={attempt.host}{msg}"
        )
        # Verbose: also dump the event as one JSON line on stderr so
        # operators can pipe to jq, log aggregators, etc.
        if verbose:
            line = {
                "step_id": node.step_id,
                "status": attempt.status.value,
                "attempt": attempt.attempt,
                "host": attempt.host,
                "message": attempt.message,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
            }
            print(json.dumps(line, sort_keys=True), file=sys.stderr)

    return emit


def _make_json_event_emitter():
    """Emit one JSON line per event to stderr.

    The final report is emitted by the deploy() function after the
    run completes. This emitter is the live-tap half of the
    `--json` story: tail it with `orchx deploy ... --json 2>>run.ndjson`
    and you have a structured event log of the run.
    """

    from orchx.engine.models import StepAttempt

    def emit(node, attempt: StepAttempt) -> None:
        line = {
            "step_id": node.step_id,
            "status": attempt.status.value,
            "attempt": attempt.attempt,
            "host": attempt.host,
            "message": attempt.message,
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
        }
        print(json.dumps(line, sort_keys=True), file=sys.stderr)

    return emit


def _report_to_dict(report) -> dict[str, object]:
    """Serialise a RunReport to a JSON-friendly dict.

    Used by `orchx deploy --json`. We deliberately flatten the
    StepAttempt objects into a list of dicts under ``attempts`` so
    that downstream tooling (CI pipelines, log aggregators) can
    reason about each attempt without chasing nested objects.
    """
    nodes: list[dict[str, object]] = []
    for step_id, node in report.plan.nodes.items():
        nodes.append(
            {
                "step_id": step_id,
                "status": node.status.value,
                "depends_on": list(node.depends_on),
                "attempts": [
                    {
                        "step_id": a.step_id,
                        "attempt": a.attempt,
                        "status": a.status.value,
                        "message": a.message,
                        "host": a.host,
                        "started_at": a.started_at,
                        "finished_at": a.finished_at,
                    }
                    for a in node.attempts
                ],
            }
        )
    return {
        "exit_code": report.exit_code,
        "aborted": report.aborted,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "topo_order": list(report.plan.topo_order),
        "nodes": nodes,
    }


def _summary(report) -> Panel:
    counts: dict[str, int] = {}
    for n in report.plan.nodes.values():
        counts[n.status.value] = counts.get(n.status.value, 0) + 1
    tbl = Table(box=None, show_header=False)
    for k in ("ok", "failed", "skipped", "rolled_back", "rollback_failed"):
        v = counts.get(k, 0)
        tbl.add_row(k, str(v))
    title = "deploy ok" if report.exit_code == 0 else "deploy failed"
    return Panel(tbl, title=title)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]aborted by user[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
