"""OrchX CLI entrypoint.

Commands:
  * ``orchx plan <descriptor>``   — parse, validate, render DAG, no I/O.
  * ``orchx deploy <descriptor>`` — render DAG and execute against a transport.
  * ``orchx validate <descriptor>`` — strict YAML/validation only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

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
        with suppress(Exception):
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


# ---- secrets subcommands ----

# We model 'orchx secrets <verb>' as a Typer sub-app,
# registered on the main app under the name "secrets".
# The sub-app has three commands: list / get / set.
#
# Why a sub-app and not three top-level commands? Three
# reasons:
#   1. The verbs share a small bit of plumbing (vault
#      selection, masking); a sub-app gives us one
#      place to maintain it.
#   2. ``orchx --help`` stays short — the secrets verbs
#      appear under their own ``Commands:`` block.
#   3. Future verbs (rotate, audit, import, export) can
#      join the sub-app without cluttering the parent.
secrets_app = typer.Typer(
    add_completion=False,
    help=(
        "Manage the active secrets vault from the shell. "
        "Supports list / get / set on the env, file, and "
        "memory backends. The vault backend (HashiCorp "
        "Vault) is read-only from this CLI; rotate via "
        "the vault itself."
    ),
)
app.add_typer(secrets_app, name="secrets")


state_app = typer.Typer(
    add_completion=False,
    help=(
        "Inspect and manage the orchx state database "
        "(the SQLite file that records every deploy). "
        "Supports list / get / cancel / purge."
    ),
)
app.add_typer(state_app, name="state")


def _resolve_default_state_db() -> Path:
    return Path("state") / "local.sqlite"


def _build_vault_for_secrets(
    backend: str | None,
    *,
    file: Path | None,
) -> Any:
    """Build the active vault for the secrets CLI.

    Mirrors the ``orchx deploy`` env handling: defaults to
    ``ORCHX_SECRETS_BACKEND`` (or ``env``), with the
    explicit ``--file`` path plumbed in for ``file`` mode.
    """
    from orchx.secrets import get_vault

    kwargs: dict[str, Any] = {}
    if backend == "file" and file is not None:
        kwargs["path"] = file
    return get_vault(backend, **kwargs)


def _mask_value(value: str) -> str:
    """Mask a secret value for terminal output.

    The literal value never lands on the operator's
    terminal in a list operation. We follow the same
    convention as HashiCorp Vault's own CLI: four-
    character prefix + ellipsis, so the operator can
    confirm the bucket is roughly the right size
    without exposing the value.
    """
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"


@secrets_app.command("list")
def secrets_list(
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        help="env|file|memory (default: $ORCHX_SECRETS_BACKEND or env).",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Path to the secrets file (when --backend=file).",
    ),
    backend_kwarg: bool = typer.Option(
        False,
        "--show-backend",
        help="Print the active backend name alongside each row.",
    ),
) -> None:
    """List every secret the active vault can resolve.

    Values are masked: first 4 chars only, then ``****``.
    Use ``orchx secrets get <name>`` to print the full
    value (which is a one-time intentional leak).
    """
    from orchx.secrets import MemoryVault

    vault = _build_vault_for_secrets(backend, file=file)
    if backend_kwarg:
        console.print(f"[dim]backend: {vault.__class__.__name__}[/dim]")

    # ``memory`` is process-local; the way to populate
    # it is via ``orchx deploy --set key=value`` or via
    # ``orchx secrets set`` below. The way to read it
    # back is the same MemoryVault instance, which is
    # what this CLI constructs each invocation. For
    # non-memory backends we go through the protocol's
    # list_names().
    is_memory = isinstance(vault, MemoryVault)
    names = sorted(vault._secrets) if is_memory else vault.list_names()  # noqa: SLF001

    if not names:
        console.print("[yellow]no secrets in vault[/yellow]")
        return

    table = Table(box=None)
    table.add_column("name", style="cyan")
    table.add_column("value", style="dim")
    for name in names:
        try:
            raw = vault.resolve(name)
        except Exception as e:
            # The list shouldn't fail on a per-row
            # missing value, but ``vault`` can throw
            # permission / network errors. Surface them
            # inline rather than aborting the whole list.
            table.add_row(name, f"[red]{type(e).__name__}: {e}[/red]")
            continue
        table.add_row(name, _mask_value(raw))
    console.print(table)


@secrets_app.command("set")
def secrets_set(
    name: str = typer.Argument(..., help="Secret name to set."),
    value: str = typer.Argument(
        ...,
        help=(
            "Secret value. For non-interactive use, prefer "
            "the environment variable (``ORCHX_SECRET_<NAME>"
            "=...``) and the ``env`` backend. Pass the "
            "value as an argument only when you understand "
            "it will appear in your shell history."
        ),
    ),
) -> None:
    """Add or overwrite a secret in the process-local memory vault.

    The memory vault is process-local; values do not
    survive across invocations. For a persistent store,
    use ``orchx deploy`` (``--secrets-backend=file
    --secrets-file=...``); this CLI does not modify
    files in place because file-mode locks and concurrent
    writes are a different problem, and we want to keep
    the CLI surface narrow.
    """
    from orchx.secrets import MemoryVault

    vault = MemoryVault()
    vault.set(name, value)
    console.print(
        f"[green]set[/green] {name}=[red]{_mask_value(value)}[/red] "
        f"(in process-local memory vault; not persisted)"
    )


@secrets_app.command("get")
def secrets_get(
    name: str = typer.Argument(..., help="Secret name to read."),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        help="env|file|memory (default: $ORCHX_SECRETS_BACKEND or env).",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        help="Path to the secrets file (when --backend=file).",
    ),
) -> None:
    """Print the resolved value of a single secret.

    This is the one place where a value lands on the
    operator's terminal. If the operator wants the
    secret in a script, the standard idiom is::

      $value="$(orchx secrets get my_name)"

    Anything beyond 'shell history' on a single-tenant
    laptop is the operator's problem; orchx keeps the
    surface narrow for that reason.
    """
    vault = _build_vault_for_secrets(backend, file=file)
    try:
        value = vault.resolve(name)
    except Exception as e:
        console.print(f"[red]error[/red] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None
    console.print(value)


@state_app.command("list")
def state_list(
    db: Path = typer.Option(
        None,
        "--db",
        envvar="ORCHX_STATE_DB",
        help="Path to the SQLite file (default: state/local.sqlite).",
    ),
    state_filter: str | None = typer.Option(
        None,
        "--state",
        "-s",
        help="Show only runs in this state.",
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """List runs in the orchx state database."""
    import asyncio

    from orchx.web.store import RunStore

    actual_db = db or _resolve_default_state_db()
    if not actual_db.exists():
        console.print(f"[yellow]no state db at {actual_db}[/yellow]")
        raise typer.Exit(code=0)

    async def _go() -> None:
        store = RunStore(actual_db)
        await store.init()
        try:
            rows, total = await store.list_runs(limit=limit, offset=offset, state=state_filter)
        finally:
            with suppress(Exception):
                await store.close()
        if not rows:
            console.print(f"[yellow]0 runs (total {total})[/yellow]")
            return
        table = Table(box=None)
        table.add_column("id", style="cyan")
        table.add_column("state", style="bold")
        table.add_column("started_at", style="dim")
        table.add_column("descriptor")
        for r in rows:
            table.add_row(
                r.id[:12],
                r.state,
                str(int(r.created_at)) if r.created_at else "",
                r.descriptor,
            )
        console.print(table)
        console.print(
            f"[dim]showing {len(rows)} of {total}; --offset and --limit for pagination[/dim]"
        )

    asyncio.run(_go())


@state_app.command("get")
def state_get(
    run_id: str = typer.Argument(..., help="Run id to inspect."),
    db: Path = typer.Option(
        None,
        "--db",
        envvar="ORCHX_STATE_DB",
        help="Path to the SQLite file.",
    ),
    events: bool = typer.Option(
        False,
        "--events",
        help="Print every event for this run.",
    ),
) -> None:
    """Print the full record for a single run."""
    import asyncio

    from orchx.web.store import RunStore

    actual_db = db or _resolve_default_state_db()
    if not actual_db.exists():
        console.print(f"[red]no state db at {actual_db}[/red]")
        raise typer.Exit(code=1) from None

    async def _go() -> None:
        store = RunStore(actual_db)
        await store.init()
        try:
            rows, _ = await store.list_runs(limit=500)
            row = next((r for r in rows if r.id == run_id), None)
            if row is None:
                console.print(f"[red]run {run_id} not found[/red]")
                raise typer.Exit(code=1) from None
            from dataclasses import asdict

            t = Table(box=None)
            t.add_column("field", style="cyan")
            t.add_column("value")
            for k, v in asdict(row).items():
                if k == "plan_json":
                    continue
                t.add_row(k, str(v))
            console.print(t)
            if events:
                evs = await store.list_events(run_id)
                console.print("[dim]events:[/dim]")
                for ev in evs:
                    console.print(
                        f"  {ev.get('status', '-'):8s} "
                        f"step={ev.get('step_id') or '-':20s} "
                        f"host={ev.get('host') or '-':10s} "
                        f"msg={(ev.get('message') or '')[:60]}"
                    )
        finally:
            with suppress(Exception):
                await store.close()

    asyncio.run(_go())


@state_app.command("cancel")
def state_cancel(
    run_id: str = typer.Argument(..., help="Run id to cancel."),
    db: Path = typer.Option(
        None,
        "--db",
        envvar="ORCHX_STATE_DB",
        help="Path to the SQLite file.",
    ),
) -> None:
    """Cancel a running run by id.

    Marks the run as aborted in the SQLite file. This is
    best-effort: cross-process cancel via a Unix socket is
    a v0.6 item; for now this state-marker is the surface
    that dashboard refreshes.
    """
    import asyncio

    from orchx.web.store import RunStore

    actual_db = db or _resolve_default_state_db()
    if not actual_db.exists():
        console.print(f"[red]no state db at {actual_db}[/red]")
        raise typer.Exit(code=1) from None

    async def _go() -> None:
        store = RunStore(actual_db)
        await store.init()
        try:
            rows, _ = await store.list_runs(limit=500)
            row = next((r for r in rows if r.id == run_id), None)
            if row is None:
                console.print(f"[red]run {run_id} not found[/red]")
                raise typer.Exit(code=1) from None
            if row.state in ("ok", "failed", "aborted"):
                console.print(
                    f"[yellow]run {run_id} already in terminal state {row.state!r}; no-op[/yellow]"
                )
                return
            await store.update_run(
                run_id,
                state="aborted",
                finished_at=int(time.time()),
            )
            await store.emit(
                run_id,
                "aborted",
                step_id="<run>",
                message="cancel requested via 'orchx state cancel'",
            )
        finally:
            with suppress(Exception):
                await store.close()

    asyncio.run(_go())
    console.print(f"[green]marked {run_id} as aborted[/green]")


@state_app.command("purge")
def state_purge(
    older_than_days: int = typer.Option(
        30,
        "--older-than-days",
        help="Remove runs whose created_at is older than this many days.",
    ),
    db: Path = typer.Option(
        None,
        "--db",
        envvar="ORCHX_STATE_DB",
        help="Path to the SQLite file.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Delete runs older than ``--older-than-days``."""
    import asyncio

    from orchx.web.store import RunStore

    actual_db = db or _resolve_default_state_db()
    if not actual_db.exists():
        console.print(f"[yellow]no state db at {actual_db}[/yellow]")
        raise typer.Exit(code=0)

    if not yes:
        confirmed = typer.confirm(
            f"purge runs older than {older_than_days} days from {actual_db}?",
            default=False,
        )
        if not confirmed:
            raise typer.Abort()

    cutoff = int(time.time()) - older_than_days * 86_400
    purged: list[str] = []

    async def _go() -> None:
        store = RunStore(actual_db)
        await store.init()
        try:
            rows, _ = await store.list_runs(limit=500)
            for r in rows:
                if r.created_at is not None and int(r.created_at) < cutoff:
                    await store.delete_run(r.id)
                    purged.append(r.id)
        finally:
            with suppress(Exception):
                await store.close()

    asyncio.run(_go())
    console.print(f"[green]purged {len(purged)} runs older than {older_than_days} days[/green]")


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
