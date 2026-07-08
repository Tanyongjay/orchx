"""Pydantic schemas for the deploy descriptor.

A descriptor is a single YAML document that fully describes a deployable
system: what roles it has, which steps run on which role, in what order.
All names are intentionally generic so that the same engine works for any
vendor.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator

# Each step's `type` field uses Literal so pydantic's discriminator
# mechanism can route on it without coercing an Enum.
_StepTypeLiteral = Literal[
    "check",
    "powershell",
    "command",
    "com-register",
    "com-unregister",
    "iis-site",
    "iis-site-remove",
    "sql",
    "package",
    "http",
    "healthcheck",
    "noop",
]


class StepType(StrEnum):
    CHECK = "check"
    POWERSHELL = "powershell"
    COMMAND = "command"
    COM_REGISTER = "com-register"
    COM_UNREGISTER = "com-unregister"
    IIS_SITE = "iis-site"
    IIS_SITE_REMOVE = "iis-site-remove"
    SQL = "sql"
    PACKAGE = "package"
    HTTP = "http"
    HEALTHCHECK = "healthcheck"
    NOOP = "noop"


class StepBase(BaseModel):
    """Common fields for every step kind."""

    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z][a-zA-Z0-9_\-.:]*$")
    type: StepType
    on_host: str | None = Field(
        default=None,
        description=(
            "Role or node selector to which this step is bound. None means the "
            "step binds to the orchestrator's 'control' role and runs locally."
        ),
    )
    needs: list[str] = Field(
        default_factory=list,
        description=("Step IDs that must reach 'ok' before this step runs. Forms the DAG."),
    )
    needs_role_state: list[str] = Field(
        default_factory=list,
        description=(
            "Per-role gate: each entry '<role>:<step_id>' means that step_id "
            "must be ok on that role before this step starts."
        ),
    )
    retries: int = Field(default=0, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=2.0, ge=0.0)
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    description: str | None = None


class CheckStep(StepBase):
    """Assert a precondition; never mutates state."""

    type: StepType = StepType.CHECK
    check: dict[str, Any] = Field(
        description="Vendor-neutral assertion: {kind: file|registry|service|env, target: ...}",
    )


class PowershellStep(StepBase):
    """Execute a PowerShell script on the target host."""

    type: StepType = StepType.POWERSHELL
    script: str | None = None
    script_file: str | None = None
    args: list[str] = Field(default_factory=list)

    @field_validator("script_file")
    @classmethod
    def _must_have_one(cls, v: str | None, info: Any) -> str | None:
        data = info.data
        if not data.get("script") and not v:
            raise ValueError("powershell step requires either 'script' or 'script_file'")
        return v


class CommandStep(StepBase):
    """Execute a generic shell command."""

    type: StepType = StepType.COMMAND
    cmd: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ComRegisterStep(StepBase):
    """Register a native COM bridge DLL (Windows only)."""

    type: StepType = StepType.COM_REGISTER
    file: str
    arch: str = Field(default="x86", pattern=r"^(x86|x64|both)$")
    fail_if_missing: bool = True


class ComUnregisterStep(StepBase):
    """Unregister a previously registered COM bridge."""

    type: StepType = StepType.COM_UNREGISTER
    file: str


class IisSiteStep(StepBase):
    """Create or update an IIS site."""

    type: StepType = StepType.IIS_SITE
    site_name: str
    physical_path: str
    port: int = Field(ge=1, le=65535)
    application_pool: str | None = None
    enable_32bit: bool = False
    parent_paths: bool = True
    host_name: str | None = None
    bindings: list[dict[str, Any]] = Field(default_factory=list)


class IisSiteRemoveStep(StepBase):
    """Remove an IIS site (rollback helper)."""

    type: StepType = StepType.IIS_SITE_REMOVE
    site_name: str


class SqlStep(StepBase):
    """Run a SQL statement against a target DB. Idempotent when written that way."""

    type: StepType = StepType.SQL
    server: str | None = None
    database: str | None = None
    sql: str | None = None
    sql_file: str | None = None
    use_windows_auth: bool = True

    @field_validator("sql_file")
    @classmethod
    def _must_have_one(cls, v: str | None, info: Any) -> str | None:
        data = info.data
        if not data.get("sql") and not v:
            raise ValueError("sql step requires either 'sql' or 'sql_file'")
        return v


class PackageStep(StepBase):
    """Stage and apply a vendor package/upgrade zip."""

    type: StepType = StepType.PACKAGE
    src: str
    runner: str | None = None  # path to the runner exe inside the staged artifact
    unpack_root: str | None = None


class HttpStep(StepBase):
    """Run an HTTP request against a target URL."""

    type: StepType = StepType.HTTP
    method: str = Field(default="GET", pattern=r"^(GET|POST|PUT|DELETE|PATCH)$")
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    expect_status: int = Field(default=200, ge=100, le=599)


class HealthcheckStep(StepBase):
    """Poll a URL until it returns expect_status or until timeout."""

    type: StepType = StepType.HEALTHCHECK
    url: str
    expect_status: int = 200
    interval_seconds: float = 5.0
    max_attempts: int = 60


class NoopStep(StepBase):
    """A step that does nothing — useful as a structural anchor."""

    type: StepType = StepType.NOOP


StepSpec = Union[
    CheckStep,
    PowershellStep,
    CommandStep,
    ComRegisterStep,
    ComUnregisterStep,
    IisSiteStep,
    IisSiteRemoveStep,
    SqlStep,
    PackageStep,
    HttpStep,
    HealthcheckStep,
    NoopStep,
]  # selected by Pydantic's smart union (default order = declared order)


class RoleSpec(BaseModel):
    """A logical role in the topology — web, db, redis, control, ..."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_\-]*$")
    count: int = Field(default=1, ge=1, le=64)
    description: str | None = None


class NodeSpec(BaseModel):
    """A concrete host binding (filled in at run time, not strictly required in YAML)."""

    role: str
    address: str
    credentials: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class TopologySpec(BaseModel):
    roles: list[RoleSpec]
    nodes: list[NodeSpec] = Field(default_factory=list)


class SystemInfo(BaseModel):
    """System metadata declared by the descriptor."""

    name: str = Field(min_length=1, max_length=120)
    code: str = Field(
        min_length=2,
        max_length=40,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Short identifier used in DB names, paths, and tags.",
    )
    version: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._\-]+$")


class Descriptor(BaseModel):
    """Top-level descriptor."""

    system: SystemInfo
    topology: TopologySpec
    steps: list[StepSpec] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Default values referenced as {{ defaults.* }} in templates.",
    )
    install_root: Path | None = None
    variables: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Free-form variables used in {{ var.x }} substitution.",
    )

    @field_validator("steps")
    @classmethod
    def _step_ids_unique(cls, v: list[StepSpec]) -> list[StepSpec]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate step ids: {dup}")
        return v

    def role_names(self) -> set[str]:
        return {r.name for r in self.topology.roles}
