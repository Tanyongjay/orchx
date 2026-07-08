"""In-memory MockTransport used by tests and `--target mock://...` deploys.

The mock never touches any real machine. It records every action in an
in-memory journal that tests assert against, and it models realistic
failure modes via a small chaos knob so we can exercise the engine's
retry / rollback paths.

This is the only transport in the default `orchx` install. Real
transports (WinRM/SSH) are opt-in.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchx.transports.base import (
    CommandResult,
    FileTransfer,
    HttpSendRequest,
    HttpSendResult,
    IisSiteSpec,
    RegisterOptions,
    SqlRunResult,
    Transport,
)


@dataclass
class MockJournal:
    """Append-only record of every action performed against the mock."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, host: str, action: str, **details: Any) -> None:
        self.entries.append({"host": host, "action": action, "details": details})

    def actions_for(self, host: str, action: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["host"] == host and e["action"] == action]


# Failure injection: a small static config for tests/CLI:
#   host -> { action -> { "fail_with": exit_code, "fail_times": int } }
# Used via `ORCHX_MOCK_CHAOS` JSON env or the engine's `--chaos` flag.


@dataclass
class MockConfig:
    fail_on: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | None) -> MockConfig:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"ORCHX_MOCK_CHAOS invalid JSON: {e}") from e
        out = cls()
        for host, items in data.items():
            out.fail_on[host] = [
                (item["action"], item["exit_code"], int(item["fail_times"])) for item in items
            ]
        return out


class MockTransport(Transport):
    """Deterministic in-memory transport.

    Usage::

        t = MockTransport()
        res = await t.run_powershell("web-1", "Write-Host hi")
        assert res.ok
        last = t.journal.actions_for("web-1", "powershell")[-1]
        assert last["details"]["script"] == "Write-Host hi"
    """

    name = "mock"

    def __init__(self, config: MockConfig | None = None, root: str | None = None) -> None:
        self.journal = MockJournal()
        self.config = config or MockConfig()
        self._fail_count: dict[str, int] = {}
        self._filesystem: dict[str, dict[str, str]] = {}  # host -> {path: contents}
        self._services: dict[str, set[str]] = {}  # host -> {site_name}
        self._registry: dict[tuple[str, str], bool] = {}  # (host, dll) -> registered
        self._databases: dict[str, set[str]] = {}  # server -> {db_name}
        self.root = root or "/var/orchx/mock"
        self._lock = asyncio.Lock()

    # ---- helpers ----

    def _maybe_fail(self, host: str, action: str, success_exit: int = 0) -> int:
        key = f"{host}::{action}"
        rules = self.config.fail_on.get(host, [])
        for act, exit_code, fail_times in rules:
            if act != action:
                continue
            seen = self._fail_count.get(key + f"::{act}", 0)
            if seen < fail_times:
                self._fail_count[key + f"::{act}"] = seen + 1
                return exit_code
        return success_exit

    # ---- Transport surface ----

    async def run_powershell(
        self, host: str, script: str, *, timeout_s: int = 600
    ) -> CommandResult:
        self.journal.record(host, "powershell", script=script, timeout_s=timeout_s)
        ec = self._maybe_fail(host, "powershell")
        return CommandResult(exit_code=ec, stdout="PS>", stderr="")

    async def run_command(
        self, host: str, cmd: list[str], *, timeout_s: int = 600
    ) -> CommandResult:
        self.journal.record(host, "command", cmd=cmd, timeout_s=timeout_s)
        ec = self._maybe_fail(host, "command")
        return CommandResult(exit_code=ec)

    async def transfer_files(self, host: str, transfers: list[FileTransfer]) -> None:
        # If chaos targets the logical `package` action, treat any
        # transfer during `package` as the trigger. We have no separate
        # `package` action, so the executor's package step routes through
        # transfer_files; allow chaos to interrupt it here.
        ec = self._maybe_fail(host, "package")
        if ec != 0:
            self.journal.record(host, "package-failed", count=len(transfers))
            raise RuntimeError(f"mock-failure action=package exit={ec}")
        async with self._lock:
            fs = self._filesystem.setdefault(host, {})
            for t in transfers:
                key = t.remote_dest
                fs[key] = f"<mock-staged:{Path(t.local_src).name}>"
        self.journal.record(host, "transfer", count=len(transfers))

    async def register_com(self, host: str, opts: RegisterOptions) -> CommandResult:
        self.journal.record(host, "com-register", **asdict(opts))
        ec = self._maybe_fail(host, "com-register")
        if ec == 0:
            self._registry[(host, opts.file)] = True
        return CommandResult(exit_code=ec)

    async def unregister_com(self, host: str, opts: RegisterOptions) -> CommandResult:
        self.journal.record(host, "com-unregister", **asdict(opts))
        ec = self._maybe_fail(host, "com-unregister")
        if ec == 0:
            self._registry[(host, opts.file)] = False
        return CommandResult(exit_code=ec)

    async def upsert_iis_site(self, host: str, spec: IisSiteSpec) -> CommandResult:
        self.journal.record(host, "iis-site", **asdict(spec))
        ec = self._maybe_fail(host, "iis-site")
        if ec == 0:
            self._services.setdefault(host, set()).add(spec.site_name)
        return CommandResult(exit_code=ec)

    async def remove_iis_site(self, host: str, site_name: str) -> CommandResult:
        self.journal.record(host, "iis-site-remove", site_name=site_name)
        ec = self._maybe_fail(host, "iis-site-remove")
        if ec == 0:
            self._services.get(host, set()).discard(site_name)
        return CommandResult(exit_code=ec)

    async def run_sql(
        self,
        host: str,
        *,
        server: str,
        database: str | None,
        sql: str,
        use_windows_auth: bool = True,
    ) -> SqlRunResult:
        self.journal.record(
            host,
            "sql",
            server=server,
            database=database,
            sql=sql,
            use_windows_auth=use_windows_auth,
        )
        ec = self._maybe_fail(host, "sql")
        if ec != 0:
            return SqlRunResult(success=False, message="mock-failure")
        # Track CREATE DATABASE effects for idempotency assertions.
        s = sql.strip().lower()
        if s.startswith("create database"):
            # Naive parse: take the token after 'create database'.
            name = s.split()[2].rstrip(";").strip("[]'`\"")
            self._databases.setdefault(server, set()).add(name)
        return SqlRunResult(success=True, message="mock-ok")

    async def send_http(self, host: str, req: HttpSendRequest) -> HttpSendResult:
        self.journal.record(host, "http", **asdict(req))
        ec = self._maybe_fail(host, "http")
        if ec != 0:
            return HttpSendResult(status=502, body="mock-failure")
        return HttpSendResult(status=200, body='{"ok": true}')

    async def file_exists(self, host: str, path: str) -> bool:
        return path in self._filesystem.get(host, {})

    async def close(self) -> None:
        return None

    # ---- test convenience ----

    def new_journal_token(self) -> str:
        return uuid.uuid4().hex
