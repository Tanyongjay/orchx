"""Transport protocol — every transport must satisfy this surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class CommandResult:
    """Result of a shell/PowerShell invocation."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class FileTransfer:
    """A logical file movement from orchestrator to a target host."""

    local_src: str
    remote_dest: str
    recursive: bool = False


@dataclass
class RegisterOptions:
    """Options for native bridge (COM/DLL) registration."""

    file: str
    arch: str = "x86"  # x86 | x64 | both


@dataclass
class IisSiteSpec:
    site_name: str
    physical_path: str
    port: int
    application_pool: str | None = None
    enable_32bit: bool = False
    parent_paths: bool = True
    bindings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SqlRunResult:
    """Result of a SQL execution."""

    success: bool
    affected_rows: int = 0
    message: str = ""


@dataclass
class HttpSendRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None


@dataclass
class HttpSendResult:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class Transport(Protocol):
    """The transport contract."""

    name: str

    async def run_powershell(
        self, host: str, script: str, *, timeout_s: int = 600
    ) -> CommandResult: ...

    async def run_command(
        self, host: str, cmd: list[str], *, timeout_s: int = 600
    ) -> CommandResult: ...

    async def transfer_files(self, host: str, transfers: list[FileTransfer]) -> None: ...

    async def register_com(self, host: str, opts: RegisterOptions) -> CommandResult: ...

    async def unregister_com(self, host: str, opts: RegisterOptions) -> CommandResult: ...

    async def upsert_iis_site(self, host: str, spec: IisSiteSpec) -> CommandResult: ...

    async def remove_iis_site(self, host: str, site_name: str) -> CommandResult: ...

    async def run_sql(
        self,
        host: str,
        *,
        server: str,
        database: str | None,
        sql: str,
        use_windows_auth: bool = True,
    ) -> SqlRunResult: ...

    async def send_http(self, host: str, req: HttpSendRequest) -> HttpSendResult: ...

    async def file_exists(self, host: str, path: str) -> bool: ...

    async def close(self) -> None: ...
