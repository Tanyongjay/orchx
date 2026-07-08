"""WinRM transport — talks to real Windows hosts via pywinrm.

This is an opt-in transport. It is NOT imported at the top of the
package; it is loaded lazily when a ``winrm://...`` URI is requested,
so the base install does not require ``pywinrm``.

Implementation notes:
  * The synchronous ``pywinrm`` session is wrapped with
    ``asyncio.to_thread`` so the event loop never blocks.
  * URI form: ``winrm://user:password@host:port`` (HTTPS by default;
    ``winrm-http://`` for HTTP-only test targets).
  * File transfer uses PowerShell ``[IO.File]::WriteAllBytes`` from
    a base64 blob carried in the environment — small files only;
    large artifacts should be pre-staged via SMB / HTTP and then
    ``unpack`` is called on the remote side.
  * All real-host actions are wrapped in ``try/except`` that converts
    pywinrm-level failures into ``CommandResult`` / ``HttpSendResult``
    so the executor and tests see a uniform shape.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

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


class WinRMTransportError(RuntimeError):
    """Raised when a real WinRM call cannot even be set up."""


@dataclass
class _Creds:
    user: str
    password: str
    host: str
    port: int
    use_ssl: bool = True
    verify_ssl: bool = True

    @property
    def endpoint(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}/wsman"


def _read_bytes_blocking(path: str) -> bytes:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as f:
        return f.read()


def _parse_winrm_uri(uri: str) -> _Creds:
    """Parse ``winrm://user:password@host:port`` into a credential bundle.

    Password characters that need escaping (``@``, ``:``, ``/``, ``?``)
    must be percent-encoded by the caller.
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("winrm", "winrm-http"):
        raise WinRMTransportError(f"not a winrm URI: {uri!r}")
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not user or not password:
        raise WinRMTransportError(f"winrm URI must include user:password@... (got: {uri!r})")
    host = parsed.hostname or ""
    if not host:
        raise WinRMTransportError(f"winrm URI missing host: {uri!r}")
    port = parsed.port or (5986 if parsed.scheme == "winrm" else 5985)
    return _Creds(
        user=user,
        password=password,
        host=host,
        port=port,
        use_ssl=(parsed.scheme == "winrm"),
        verify_ssl=True,
    )


class WinRMTransport(Transport):
    """Real Windows host transport.

    The transport is stateful: it holds an open session per host
    address (one ``Session`` per host) and reuses it across calls.
    Sessions are wrapped with ``asyncio.to_thread`` to keep
    ``pywinrm``'s blocking calls off the event loop.
    """

    name = "winrm"

    def __init__(self, uri: str) -> None:
        try:
            import winrm  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as e:
            raise WinRMTransportError(
                "pywinrm is required for winrm:// transports; "
                "install with `uv pip install -e .[real]`"
            ) from e
        self.uri = uri
        self.creds = _parse_winrm_uri(uri)
        self._sessions: dict[str, Any] = {}  # host -> winrm.Session

    # ---- session mgmt ----

    def _session(self) -> Any:
        from winrm import Session  # type: ignore[import-not-found]

        key = self.creds.host
        sess = self._sessions.get(key)
        if sess is not None:
            return sess
        sess = Session(
            target=self.creds.endpoint,
            auth=(self.creds.user, self.creds.password),
            server_cert_validation=("validate" if self.creds.verify_ssl else "ignore"),
        )
        self._sessions[key] = sess
        return sess

    async def _run_ps(self, script: str, timeout_s: int) -> CommandResult:
        def call() -> tuple[int, str, str]:
            sess = self._session()
            ps = sess.run_ps(script)
            return (
                int(ps.status_code),
                (ps.std_out or b"").decode("utf-8", "replace"),
                (ps.std_err or b"").decode("utf-8", "replace"),
            )

        try:
            ec, out, err = await asyncio.wait_for(
                asyncio.to_thread(call),
                timeout=timeout_s,
            )
            return CommandResult(exit_code=ec, stdout=out, stderr=err)
        except TimeoutError as e:
            return CommandResult(exit_code=124, stderr=f"timeout: {e}")
        except Exception as e:
            return CommandResult(exit_code=1, stderr=f"{type(e).__name__}: {e}")

    async def _run_cmd(self, cmd: list[str], timeout_s: int) -> CommandResult:
        joined = " ".join(shlex.quote(p) for p in cmd)
        return await self._run_ps(f"& {joined}", timeout_s)

    async def _run_ps_with_env(
        self,
        script: str,
        *,
        env: dict[str, str],
        timeout_s: int,
    ) -> CommandResult:
        # pywinrm's run_ps doesn't take env, so we set $env:X inline.
        prefix = "\n".join(
            f"$env:{k} = '{v.replace(chr(39), chr(39) + chr(39))}'" for k, v in env.items()
        )
        return await self._run_ps(prefix + "\n" + script, timeout_s)

    # ---- transport surface ----

    async def run_powershell(
        self,
        host: str,
        script: str,
        *,
        timeout_s: int = 600,
    ) -> CommandResult:
        return await self._run_ps(script, timeout_s)

    async def run_command(
        self,
        host: str,
        cmd: list[str],
        *,
        timeout_s: int = 600,
    ) -> CommandResult:
        return await self._run_cmd(cmd, timeout_s)

    async def transfer_files(
        self,
        host: str,
        transfers: list[FileTransfer],
    ) -> None:
        for t in transfers:
            local = t.local_src
            remote = t.remote_dest
            blob = await asyncio.to_thread(_read_bytes_blocking, local)
            b64 = base64.b64encode(blob).decode("ascii")
            ps = (
                "$b64 = $env:ORCHX_FILE_B64;"
                "$bytes = [Convert]::FromBase64String($b64);"
                f"$dir = Split-Path -Parent '{remote}';"
                "if (-not (Test-Path -LiteralPath $dir)) { "
                "  New-Item -ItemType Directory -Path $dir -Force | Out-Null "
                "};"
                f"[IO.File]::WriteAllBytes('{remote}', $bytes);"
                f"Write-Host ('wrote ' + "
                f"(Get-Item -LiteralPath '{remote}').Length + ' bytes')"
            )
            env = os.environ.copy()
            env["ORCHX_FILE_B64"] = b64
            res = await self._run_ps_with_env(ps, env=env, timeout_s=600)
            if res.exit_code != 0:
                raise RuntimeError(f"transfer {local} -> {remote} failed: {res.stderr}")

    async def register_com(
        self,
        host: str,
        opts: RegisterOptions,
    ) -> CommandResult:
        sysdir = "C:\\Windows\\System32" if opts.arch == "x64" else "C:\\Windows\\SysWOW64"
        script = (
            f"Set-Location '{sysdir}'; ./regsvr32.exe /s '{opts.file}'; Write-Host $LASTEXITCODE"
        )
        return await self._run_ps(script, timeout_s=300)

    async def unregister_com(
        self,
        host: str,
        opts: RegisterOptions,
    ) -> CommandResult:
        sysdir = "C:\\Windows\\System32" if opts.arch == "x64" else "C:\\Windows\\SysWOW64"
        script = (
            f"Set-Location '{sysdir}'; ./regsvr32.exe /s /u '{opts.file}'; Write-Host $LASTEXITCODE"
        )
        return await self._run_ps(script, timeout_s=300)

    async def upsert_iis_site(
        self,
        host: str,
        spec: IisSiteSpec,
    ) -> CommandResult:
        enable_32 = "$true" if spec.enable_32bit else "$false"
        parent = "$true" if spec.parent_paths else "$false"
        site = spec.site_name
        path = spec.physical_path
        port = spec.port
        app_pool = spec.application_pool or "DefaultAppPool"
        script = (
            "Import-Module WebAdministration -ErrorAction SilentlyContinue;"
            f"if (-not (Test-Path 'IIS:\\Sites\\{site}')) {{"
            f"  New-Item 'IIS:\\Sites\\{site}' "
            f"    -PhysicalPath '{path}' "
            f"    -Bindings @{{protocol='http';port='{port}'}} "
            "    -Force | Out-Null;"
            f"}}"
            f"Set-ItemProperty 'IIS:\\Sites\\{site}' "
            f"  -Name 'applicationPool' -Value '{app_pool}';"
            f"$pool = 'IIS:\\AppPools\\' + "
            f"  (Get-Item 'IIS:\\Sites\\{site}').applicationPool;"
            f"Set-ItemProperty $pool "
            f"  -Name 'enable32BitAppOnWin64' -Value {enable_32};"
            f"Set-WebConfigurationProperty '/system.webServer/asp' "
            f"  -Name 'enableParentPaths' -Value {parent} | Out-Null;"
            "Write-Host 'OK'"
        )
        return await self._run_ps(script, timeout_s=300)

    async def remove_iis_site(
        self,
        host: str,
        site_name: str,
    ) -> CommandResult:
        script = (
            "Import-Module WebAdministration -ErrorAction SilentlyContinue;"
            f"if (Test-Path 'IIS:\\Sites\\{site_name}') {{"
            f"  Remove-Website -Name '{site_name}' -ErrorAction SilentlyContinue"
            "}}"
        )
        return await self._run_ps(script, timeout_s=300)

    async def run_sql(
        self,
        host: str,
        *,
        server: str,
        database: str | None,
        sql: str,
        use_windows_auth: bool = True,
    ) -> SqlRunResult:
        auth_part = "Integrated Security=SSPI" if use_windows_auth else "User ID=...;Password=...;"
        db_part = f"Database='{database}';" if database else ""
        # MVP: only integrated auth; SQL auth requires caller-injected
        # connection strings and is deferred to a later stage.
        script = (
            f"$conn = New-Object System.Data.SqlClient.SqlConnection "
            f"('Server={server};{db_part}{auth_part}');"
            f"$cmd = $conn.CreateCommand();"
            f'$cmd.CommandText = @"\n{sql}\n"@;'
            f"$conn.Open();"
            f'try {{ $r = $cmd.ExecuteNonQuery(); Write-Host "rows=$r" }} '
            "finally { $conn.Close() }"
        )
        res = await self._run_ps(script, timeout_s=300)
        return SqlRunResult(
            success=(res.exit_code == 0),
            message=res.stdout.strip() or res.stderr.strip(),
        )

    async def send_http(
        self,
        host: str,
        req: HttpSendRequest,
    ) -> HttpSendResult:
        # Use PowerShell Invoke-WebRequest so we don't have to install
        # a Python HTTP client on the remote host.
        body = req.body or ""
        if body:
            b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
            body_var = (
                f"$body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))"
            )
        else:
            body_var = "$body = ''"
        header_lines = "\n".join(
            f"$h['{k}'] = '{v.replace(chr(39), chr(39) + chr(39))}'" for k, v in req.headers.items()
        )
        script = (
            "$ProgressPreference = 'SilentlyContinue';"
            f"{body_var};"
            "$h = @{}; " + (header_lines + ";" if header_lines else "") + f"try {{"
            f"  $r = Invoke-WebRequest -UseBasicParsing "
            f"    -Uri '{req.url}' -Method '{req.method}' "
            f"    -Headers $h -Body $body -TimeoutSec 30;"
            f"  Write-Host $r.StatusCode;"
            f"  Write-Host '---';"
            f"  Write-Host $r.Content"
            f"}} catch {{"
            f"  if ($_.Exception.Response) {{"
            f"    Write-Host $_.Exception.Response.StatusCode.Value__"
            f"  }} else {{ Write-Host 0 }}"
            f"}}"
        )
        res = await self._run_ps(script, timeout_s=60)
        body_out = res.stdout
        status = 0
        if body_out:
            first, _, rest = body_out.partition("---")
            try:
                status = int(first.strip())
            except ValueError:
                status = 0
            body_out = rest
        return HttpSendResult(status=status, body=body_out or "")

    async def file_exists(self, host: str, path: str) -> bool:
        res = await self._run_ps(
            f"if (Test-Path -LiteralPath '{path}') {{ exit 0 }} else {{ exit 1 }}",
            timeout_s=60,
        )
        return res.exit_code == 0

    async def tcp_open(self, host: str, target: str) -> int:
        # target is a tcp://host:port URL. We hand it to PowerShell
        # which has System.Net.Sockets.TcpClient.
        # host:port is the only thing we can probe remotely; in MVP-1
        # we ignore the orchestrator-side `host` argument (we are already
        # running on the host the user asked us to talk to).
        spec = target[len("tcp://") :]
        script = (
            "try {"
            "  $client = New-Object System.Net.Sockets.TcpClient;"
            f"  $iar = $client.BeginConnect('{spec.split(':')[0]}', "
            f"    [int]'{spec.split(':')[1]}', $null, $null);"
            "  $ok = $iar.AsyncWaitHandle.WaitOne(5000, $false);"
            "  if (-not $ok) { exit 1 }"
            "  $client.EndConnect($iar);"
            "  $client.Close();"
            "  exit 0"
            "} catch { exit 1 }"
        )
        res = await self._run_ps(script, timeout_s=10)
        return 200 if res.exit_code == 0 else 0

    async def close(self) -> None:
        # winrm.Session has no .close(); drop our refs and let GC collect.
        self._sessions.clear()
