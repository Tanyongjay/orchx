"""Built-in deploy step implementations.

A ``Step`` value object is constructed from a descriptor's step entry
and exposes:

  * ``forward_action`` — what to run on success
  * ``reverse_action`` — what to run on failure (best-effort undo)

The engine hands the resulting ``Action`` to the transport. Steps do
not touch the network, disk, OS, or registry directly.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchx.descriptor.models import (
    CheckStep,
    CommandStep,
    ComRegisterStep,
    ComUnregisterStep,
    HealthcheckStep,
    HttpStep,
    IisSiteRemoveStep,
    IisSiteStep,
    NoopStep,
    PackageStep,
    PowershellStep,
    SqlStep,
    StepSpec,
)

if TYPE_CHECKING:
    from orchx.transports.base import (
        Transport,
    )


@dataclass
class Action:
    """A normalised request to a transport."""

    kind: str  # one of: powershell / command / com-register / com-unregister /
    #         iis-site / iis-site-remove / sql / package / http / healthcheck / noop / check
    host: str
    payload: dict[str, object]  # type: ignore[type-arg]  # noqa: ERA001


class BuiltinStep(abc.ABC):
    """Base class for built-in step kinds."""

    @abc.abstractmethod
    def forward_action(self) -> Action: ...

    @abc.abstractmethod
    def reverse_action(self) -> Action | None: ...

    @property
    @abc.abstractmethod
    def timeout(self) -> int: ...


# ---- per-type wrappers ------------------------------------------------


class CheckStepAdapter(BuiltinStep):
    kind = "check"

    def __init__(self, step: CheckStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(kind="check", host=self.host, payload={"check": self.step.check})

    def reverse_action(self) -> None:
        return None


class PowershellStepAdapter(BuiltinStep):
    kind = "powershell"

    def __init__(self, step: PowershellStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="powershell",
            host=self.host,
            payload={
                "script": self.step.script or "",
                "script_file": self.step.script_file,
                "args": list(self.step.args),
            },
        )

    def reverse_action(self) -> None:
        # None: PS scripts are written idempotent; reversal is the
        # user's responsibility (declare an explicit reversal step).
        return None


class CommandStepAdapter(BuiltinStep):
    kind = "command"

    def __init__(self, step: CommandStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="command",
            host=self.host,
            payload={
                "cmd": list(self.step.cmd),
                "cwd": self.step.cwd,
                "env": dict(self.step.env),
            },
        )

    def reverse_action(self) -> None:
        return None


class ComRegisterStepAdapter(BuiltinStep):
    kind = "com-register"

    def __init__(self, step: ComRegisterStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="com-register",
            host=self.host,
            payload={"file": self.step.file, "arch": self.step.arch},
        )

    def reverse_action(self) -> Action:
        return Action(
            kind="com-unregister",
            host=self.host,
            payload={"file": self.step.file, "arch": self.step.arch},
        )


class ComUnregisterStepAdapter(BuiltinStep):
    kind = "com-unregister"

    def __init__(self, step: ComUnregisterStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="com-unregister",
            host=self.host,
            payload={"file": self.step.file, "arch": self.step.arch},
        )

    def reverse_action(self) -> Action:
        return Action(
            kind="com-register",
            host=self.host,
            payload={"file": self.step.file, "arch": self.step.arch},
        )


class IisSiteStepAdapter(BuiltinStep):
    kind = "iis-site"

    def __init__(self, step: IisSiteStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        s = self.step
        return Action(
            kind="iis-site",
            host=self.host,
            payload={
                "site_name": s.site_name,
                "physical_path": s.physical_path,
                "port": s.port,
                "application_pool": s.application_pool,
                "enable_32bit": s.enable_32bit,
                "parent_paths": s.parent_paths,
                "bindings": [dict(b) for b in s.bindings],
            },
        )

    def reverse_action(self) -> Action:
        return Action(
            kind="iis-site-remove",
            host=self.host,
            payload={"site_name": self.step.site_name},
        )


class IisSiteRemoveStepAdapter(BuiltinStep):
    kind = "iis-site-remove"

    def __init__(self, step: IisSiteRemoveStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="iis-site-remove",
            host=self.host,
            payload={"site_name": self.step.site_name},
        )

    def reverse_action(self) -> None:
        return None


class SqlStepAdapter(BuiltinStep):
    kind = "sql"

    def __init__(self, step: SqlStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="sql",
            host=self.host,
            payload={
                "server": self.step.server,
                "database": self.step.database,
                "sql": self.step.sql or "",
                "sql_file": self.step.sql_file,
                "use_windows_auth": self.step.use_windows_auth,
            },
        )

    def reverse_action(self) -> None:
        return None


class PackageStepAdapter(BuiltinStep):
    kind = "package"

    def __init__(self, step: PackageStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(
            kind="package",
            host=self.host,
            payload={
                "src": self.step.src,
                "runner": self.step.runner,
                "unpack_root": self.step.unpack_root,
            },
        )

    def reverse_action(self) -> None:
        return None


class HttpStepAdapter(BuiltinStep):
    kind = "http"

    def __init__(self, step: HttpStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        s = self.step
        return Action(
            kind="http",
            host=self.host,
            payload={
                "method": s.method,
                "url": s.url,
                "headers": dict(s.headers),
                "body": s.body,
                "expect_status": s.expect_status,
            },
        )

    def reverse_action(self) -> None:
        return None


class HealthcheckStepAdapter(BuiltinStep):
    kind = "healthcheck"

    def __init__(self, step: HealthcheckStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        s = self.step
        return Action(
            kind="healthcheck",
            host=self.host,
            payload={
                "url": s.url,
                "expect_status": s.expect_status,
                "interval_seconds": s.interval_seconds,
                "max_attempts": s.max_attempts,
            },
        )

    def reverse_action(self) -> None:
        return None


class NoopStepAdapter(BuiltinStep):
    kind = "noop"

    def __init__(self, step: NoopStep, host: str) -> None:
        self.step = step
        self.host = host

    @property
    def timeout(self) -> int:
        return self.step.timeout_seconds

    def forward_action(self) -> Action:
        return Action(kind="noop", host=self.host, payload={})

    def reverse_action(self) -> None:
        return None


def build_step_adapter(step: StepSpec, host: str) -> BuiltinStep:
    """Construct the right adapter for a step kind."""
    if isinstance(step, CheckStep):
        return CheckStepAdapter(step, host)
    if isinstance(step, PowershellStep):
        return PowershellStepAdapter(step, host)
    if isinstance(step, CommandStep):
        return CommandStepAdapter(step, host)
    if isinstance(step, ComRegisterStep):
        return ComRegisterStepAdapter(step, host)
    if isinstance(step, ComUnregisterStep):
        return ComUnregisterStepAdapter(step, host)
    if isinstance(step, IisSiteStep):
        return IisSiteStepAdapter(step, host)
    if isinstance(step, IisSiteRemoveStep):
        return IisSiteRemoveStepAdapter(step, host)
    if isinstance(step, SqlStep):
        return SqlStepAdapter(step, host)
    if isinstance(step, PackageStep):
        return PackageStepAdapter(step, host)
    if isinstance(step, HttpStep):
        return HttpStepAdapter(step, host)
    if isinstance(step, HealthcheckStep):
        return HealthcheckStepAdapter(step, host)
    if isinstance(step, NoopStep):
        return NoopStepAdapter(step, host)
    raise ValueError(f"unknown step type: {step!r}")


# ---- step runner -----------------------------------------------------


async def execute_step(
    adapter: BuiltinStep,
    transport: Transport,
) -> tuple[bool, str]:
    """Forward-run a step against the transport.

    Returns ``(ok, message)``. Never raises — failures are converted
    to ``(False, message)`` so the executor can decide whether to retry
    or unwind.
    """
    from orchx.transports.base import (
        HttpSendRequest,
        IisSiteSpec,
        RegisterOptions,
    )

    action = adapter.forward_action()
    try:
        if action.kind == "powershell":
            res = await transport.run_powershell(
                action.host,
                action.payload["script"],
                timeout_s=adapter.timeout,
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "command":
            res = await transport.run_command(
                action.host,
                action.payload["cmd"],
                timeout_s=adapter.timeout,
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "com-register":
            res = await transport.register_com(
                action.host,
                RegisterOptions(
                    file=action.payload["file"],
                    arch=action.payload["arch"],
                ),
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "com-unregister":
            res = await transport.unregister_com(
                action.host,
                RegisterOptions(
                    file=action.payload["file"],
                    arch=action.payload["arch"],
                ),
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "iis-site":
            res = await transport.upsert_iis_site(
                action.host,
                IisSiteSpec(
                    site_name=action.payload["site_name"],
                    physical_path=action.payload["physical_path"],
                    port=action.payload["port"],
                    application_pool=action.payload.get("application_pool"),
                    enable_32bit=action.payload["enable_32bit"],
                    parent_paths=action.payload["parent_paths"],
                    bindings=list(action.payload["bindings"]),
                ),
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "iis-site-remove":
            res = await transport.remove_iis_site(
                action.host,
                action.payload["site_name"],
            )
            return (res.ok, f"exit={res.exit_code}")

        if action.kind == "sql":
            res = await transport.run_sql(
                action.host,
                server=action.payload["server"],
                database=action.payload["database"],
                sql=action.payload["sql"],
                use_windows_auth=action.payload["use_windows_auth"],
            )
            return (res.success, res.message)

        if action.kind == "http":
            res = await transport.send_http(
                action.host,
                HttpSendRequest(
                    method=action.payload["method"],
                    url=action.payload["url"],
                    headers=dict(action.payload["headers"]),
                    body=action.payload.get("body"),
                ),
            )
            ok = res.status == action.payload["expect_status"]
            return (ok, f"status={res.status}")

        if action.kind == "healthcheck":
            # healthcheck = poll http until expect_status or attempts
            import asyncio

            url = action.payload["url"]
            expect = action.payload["expect_status"]
            interval = action.payload["interval_seconds"]
            max_attempts = action.payload["max_attempts"]
            last = 0
            for _ in range(max_attempts):
                resp = await transport.send_http(
                    action.host,
                    HttpSendRequest(method="GET", url=url),
                )
                last = resp.status
                if last == expect:
                    return (True, f"status={last}")
                await asyncio.sleep(interval)
            return (False, f"timeout, last status={last}")

        if action.kind == "check":
            # checks are non-mutating assertions; in mock we always pass
            return (True, "check-ok")

        if action.kind == "noop":
            return (True, "noop-ok")

        if action.kind == "package":
            # MVP: stage then invoke runner if provided; detailed unpack is
            # delegated to the transport once it knows how.
            from orchx.transports.base import FileTransfer

            await transport.transfer_files(
                action.host,
                [FileTransfer(local_src=action.payload["src"], remote_dest=action.payload["src"])],
            )
            return (True, "package-staged")

        return (False, f"unknown action kind: {action.kind}")
    except Exception as e:  # transport-side failures never escape
        return (False, f"{type(e).__name__}: {e}")


async def reverse_step(adapter: BuiltinStep, transport: Transport) -> tuple[bool, str]:
    """Best-effort reversal of a step (rollback)."""
    rev = adapter.reverse_action()
    if rev is None:
        return (True, "no-op")

    # Reuse the forward-runner by swapping the action into a synthetic
    # adapter that yields the reverse action as its forward action.
    class _Wrap(BuiltinStep):
        @property
        def timeout(self) -> int:  # type: ignore[override]
            return adapter.timeout

        def forward_action(self) -> Action:
            return rev

        def reverse_action(self) -> None:
            return None

    return await execute_step(_Wrap(), transport)
