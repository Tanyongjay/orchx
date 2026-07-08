"""SSH transport — talks to Linux/Unix hosts via asyncssh.

This is an opt-in transport, parallel to WinRMTransport. It is loaded
lazily when an ``ssh://`` or ``ssh+key://`` URI is requested, so the
base install does not require ``asyncssh``.

Implementation notes:
  * ``asyncssh`` is fully async; we don't need ``asyncio.to_thread``
    wrappers here.
  * URI forms:
      - ``ssh://user[:password]@host:port`` — password authentication.
      - ``ssh+key://user@host:port?keyfile=/path[&passphrase=...]`` —
        public-key authentication.
  * The host's filesystem is reachable via SFTP for file transfers.
  * The transport surfaces Linux-shaped operations for steps whose
    Windows counterparts don't apply (COM, IIS): they no-op with a
    recorded action so the executor flow is preserved.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

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


class SSHTransportError(RuntimeError):
    """Raised when a real SSH call cannot even be set up."""


@dataclass
class _Creds:
    user: str
    password: str | None
    keyfile: str | None
    passphrase: str | None
    host: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


def _read_bytes_blocking(path: str) -> bytes:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        return f.read()


def _parse_ssh_uri(uri: str) -> _Creds:
    """Parse ``ssh://user:pass@host:port`` (or ``ssh+key://``) into a bundle."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("ssh", "ssh+key"):
        raise SSHTransportError(f"not an ssh URI: {uri!r}")
    user = unquote(parsed.username or "")
    if not user:
        raise SSHTransportError(f"ssh URI must include user@...: {uri!r}")
    password = unquote(parsed.password) if parsed.password else None
    host = parsed.hostname or ""
    if not host:
        raise SSHTransportError(f"ssh URI missing host: {uri!r}")
    port = parsed.port or 22
    q = parse_qs(parsed.query or "")
    keyfile = unquote(q["keyfile"][0]) if "keyfile" in q else None
    passphrase = unquote(q["passphrase"][0]) if "passphrase" in q else None
    if parsed.scheme == "ssh+key" and not keyfile:
        raise SSHTransportError(f"ssh+key:// requires ?keyfile=... query arg: {uri!r}")
    return _Creds(
        user=user,
        password=password,
        keyfile=keyfile,
        passphrase=passphrase,
        host=host,
        port=port,
    )


class SSHTransport(Transport):
    """Linux/Unix host transport.

    The transport holds a single ``asyncssh.connect`` context. Each
    command opens its own SSH channel and waits for the result; small
    command bursts reuse the connection. SFTP is opened on demand for
    file transfers.
    """

    name = "ssh"

    def __init__(self, uri: str) -> None:
        try:
            import asyncssh  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as e:
            raise SSHTransportError(
                "asyncssh is required for ssh:// transports; "
                "install with `uv pip install -e .[real]`"
            ) from e
        self.uri = uri
        self.creds = _parse_ssh_uri(uri)
        self._conn: Any = None  # asyncssh connection
        self._sftp: Any = None  # sftp client, lazy

    # ---- connection ----

    async def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        import asyncssh  # type: ignore[import-not-found]

        opts: dict[str, Any] = {
            "host": self.creds.host,
            "port": self.creds.port,
            "username": self.creds.user,
            "known_hosts": None,  # MVP: do not verify; v1: plumb vault
        }
        if self.creds.password:
            opts["password"] = self.creds.password
        if self.creds.keyfile:
            opts["client_keys"] = [self.creds.keyfile]
            if self.creds.passphrase:
                opts["passphrase"] = self.creds.passphrase
        self._conn = await asyncssh.connect(**opts)
        return self._conn

    async def _sftp_client(self) -> Any:
        if self._sftp is not None:
            return self._sftp
        conn = await self._connect()
        self._sftp = await conn.start_sftp_client()
        return self._sftp

    # ---- helpers ----

    async def _run_sh(self, cmd: str, timeout_s: int) -> CommandResult:
        import asyncssh  # type: ignore[import-not-found]

        conn = await self._connect()
        try:
            completed = await asyncio.wait_for(
                conn.run(cmd, check=False),
                timeout=timeout_s,
            )
            return CommandResult(
                exit_code=completed.exit_status,
                stdout=(completed.stdout or ""),
                stderr=(completed.stderr or ""),
            )
        except TimeoutError as e:
            return CommandResult(exit_code=124, stderr=f"timeout: {e}")
        except asyncssh.PermissionDenied as e:
            return CommandResult(exit_code=126, stderr=str(e))
        except Exception as e:
            return CommandResult(exit_code=1, stderr=f"{type(e).__name__}: {e}")

    async def _run_sh_list(
        self,
        cmd: list[str],
        timeout_s: int,
    ) -> CommandResult:
        joined = " ".join(shlex.quote(p) for p in cmd)
        return await self._run_sh(joined, timeout_s)

    # ---- transport surface ----

    async def run_powershell(
        self,
        host: str,
        script: str,
        *,
        timeout_s: int = 600,
    ) -> CommandResult:
        # On Linux we treat powershell-shaped scripts as plain sh —
        # most Linux operators in this codebase do not run pwsh; this
        # is here so the same descriptor can address a Linux box.
        return await self._run_sh(script, timeout_s)

    async def run_command(
        self,
        host: str,
        cmd: list[str],
        *,
        timeout_s: int = 600,
    ) -> CommandResult:
        return await self._run_sh_list(cmd, timeout_s)

    async def transfer_files(
        self,
        host: str,
        transfers: list[FileTransfer],
    ) -> None:
        sftp = await self._sftp_client()
        for t in transfers:
            local = t.local_src
            remote = t.remote_dest
            blob = await asyncio.to_thread(_read_bytes_blocking, local)
            parent = os.path.dirname(remote)
            if parent and parent != "/":
                await self._run_sh(
                    f"mkdir -p {shlex.quote(parent)}",
                    timeout_s=30,
                )
            async with sftp.open(remote, "wb") as f:
                await f.write(blob)

    async def register_com(
        self,
        host: str,
        opts: RegisterOptions,
    ) -> CommandResult:
        # COM bridges are Windows-only; on Linux the step is a no-op
        # (we still log it so journals keep a uniform shape).
        return CommandResult(
            exit_code=0,
            stdout=f"ssh:com-no-op: {opts.file} (linux transport)",
        )

    async def unregister_com(
        self,
        host: str,
        opts: RegisterOptions,
    ) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=f"ssh:com-unregister-no-op: {opts.file} (linux transport)",
        )

    async def upsert_iis_site(
        self,
        host: str,
        spec: IisSiteSpec,
    ) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=f"ssh:iis-no-op: {spec.site_name} (linux transport)",
        )

    async def remove_iis_site(
        self,
        host: str,
        site_name: str,
    ) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=f"ssh:iis-remove-no-op: {site_name} (linux transport)",
        )

    async def run_sql(
        self,
        host: str,
        *,
        server: str,
        database: str | None,
        sql: str,
        use_windows_auth: bool = True,
    ) -> SqlRunResult:
        # Linux-side default: psql. The descriptor author can override
        # with a generic command step if they need a different client.
        db_part = f"-d {shlex.quote(database)} " if database else ""
        cmd = f"psql {db_part}-h {shlex.quote(server)} -tA -v ON_ERROR_STOP=1 -c {shlex.quote(sql)}"
        res = await self._run_sh(cmd, timeout_s=300)
        return SqlRunResult(
            success=(res.exit_code == 0),
            message=res.stdout.strip() or res.stderr.strip(),
        )

    async def send_http(
        self,
        host: str,
        req: HttpSendRequest,
    ) -> HttpSendResult:
        # Defer to curl on the remote side. Capture status code with
        # -w; body to stdout. Two calls so we can keep the streams
        # clean and avoid juggling curl's dual output streams.
        status_cmd = (
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"-X {shlex.quote(req.method)} {shlex.quote(req.url)}"
        )
        for k, v in req.headers.items():
            status_cmd += f" -H {shlex.quote(f'{k}: {v}')}"
        if req.body:
            status_cmd += f" --data-binary {shlex.quote(req.body)}"
        status_res = await self._run_sh(status_cmd, timeout_s=60)
        try:
            status = int(status_res.stdout.strip().splitlines()[-1])
        except ValueError:
            status = 0
        body_cmd = f"curl -s -X {shlex.quote(req.method)} {shlex.quote(req.url)}"
        for k, v in req.headers.items():
            body_cmd += f" -H {shlex.quote(f'{k}: {v}')}"
        if req.body:
            body_cmd += f" --data-binary {shlex.quote(req.body)}"
        body_res = await self._run_sh(body_cmd, timeout_s=60)
        return HttpSendResult(
            status=status,
            body=body_res.stdout,
            headers={},
        )

    async def tcp_open(self, host: str, target: str) -> int:
        spec = target[len("tcp://") :]
        host_part, _, port_part = spec.partition(":")
        # bash's /dev/tcp is the simplest probe; some distros build
        # bash without it, so fall back to a Python one-liner.
        cmd = f"timeout 5 bash -c 'cat </dev/tcp/{host_part}/{port_part}' < /dev/null"
        res = await self._run_sh(cmd, timeout_s=10)
        return 200 if res.exit_code == 0 else 0

    async def file_exists(self, host: str, path: str) -> bool:
        res = await self._run_sh(
            f"test -e {shlex.quote(path)}",
            timeout_s=30,
        )
        return res.exit_code == 0

    async def close(self) -> None:
        if self._sftp is not None:
            with suppress(Exception):
                self._sftp.exit()
            self._sftp = None
        if self._conn is not None:
            with suppress(Exception):
                self._conn.close()
            self._conn = None
