"""OrchX CLI entrypoint.

Commands:
  * ``orchx plan <descriptor>``   — parse, validate, render DAG, no I/O.
  * ``orchx deploy <descriptor>`` — render DAG and execute against a transport.
  * ``orchx validate <descriptor>`` — strict YAML/validation only.
"""

from __future__ import annotations

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
) -> None:
    """Deploy against the chosen transport."""
    parsed = load_descriptor(descriptor)
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

    exec_ = Executor(
        descriptor=parsed,
        plan=plan_obj,
        transport=transport,
        rollback_on_failure=rollback,
        on_event=_make_event_emitter(console),
    )
    import asyncio

    report = asyncio.run(exec_.run())
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


def _make_event_emitter(console_: Console):
    from orchx.engine.models import StepAttempt

    def emit(node, attempt: StepAttempt) -> None:
        sym = {
            "ok": "[green]✓[/green]",
            "failed": "[red]✗[/red]",
            "skipped": "[yellow]~[/yellow]",
            "rolling_back": "[magenta]⟲[/magenta]",
            "rolled_back": "[green]✓ rb[/green]",
            "rollback_failed": "[red]✗ rb[/red]",
        }
        s = sym.get(attempt.status.value, f"[blue]{attempt.status.value}[/blue]")
        msg = f" {attempt.message}" if attempt.message else ""
        console_.print(
            f"  {s}  {node.step_id:24s}  try={attempt.attempt}  host={attempt.host}{msg}"
        )

    return emit


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
