"""Descriptor loader + Jinja2-style substitution.

A descriptor is mostly static YAML with one layer of templating: any
`{{ ... }}` token is expanded using (in order):
  1. inline `variables` from the descriptor
  2. `defaults.*`
  3. `system.*`
  4. CLI `--set key=value` overrides (handled by the engine, not this module)

We deliberately avoid a templating engine: substitution is needed only
in a handful of fields (paths, URLs, SQL names), and rolling our own
keeps the artifact diffable / greppable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from orchx import secrets as _secrets
from orchx.descriptor.models import Descriptor

_BLOCK = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_PATH_AND_FILTER = re.compile(
    r"^\s*"
    r"(?P<path>[a-zA-Z_][a-zA-Z0-9_\.]*)"
    r"(?:\s*\|\s*default\(\s*"
    r"(?P<defq>['\"])"
    r"(?P<defval>[^'\"]*)"
    r"(?P=defq)"
    r"\s*\))?"
    r"\s*$",
)

# Tiny filter set supported in templates. We do NOT want a full
# Jinja2 here; we only need the one filter the sample descriptors
# actually use.
_FILTER_DEFAULT = "default"


def render_template(value: Any, ctx: dict[str, Any]) -> Any:
    """Recursively substitute ``{{ name.path | default("...") }}`` inside ``value``.

    Filters supported: ``default("...")`` and ``default('...')``.
    The default is used only if the lookup raises ``KeyError``;
    a non-empty resolved value is returned as-is even if the
    literal evaluates to an empty string.
    """
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            inner = m.group(1)
            pm = _PATH_AND_FILTER.match(inner)
            if pm is None:
                raise KeyError(f"malformed template expression: {inner!r}")
            path = pm.group("path")
            default = pm.group("defval")  # may be None
            # Secret references are intentionally NOT resolved at
            # descriptor-load time. The vault must never be
            # consulted from `load_descriptor` (which is called
            # by `orchx plan`, by the web control plane when a
            # run is created, etc.) — doing so would leak resolved
            # values into the descriptor model in memory, into
            # the SQLite run log on disk, and into the dashboard
            # event stream.
            # The engine resolves `secret.*` only at the moment
            # the command is about to be sent to the transport,
            # and even then it never persists the resolved value
            # into the plan model.
            if path == "secret" or path.startswith("secret."):
                # Secrets are intentionally NOT resolved at
                # descriptor-load time — see the security
                # note in render_template's docstring and
                # tests/test_secret_template.py for the
                # lock-down test that proves no value
                # ever lands on disk from this path.
                # However, callers that need to know which
                # secret names were referenced (notably
                # ``orchx doctor`` for preflight checks)
                # can install a side channel by passing a
                # ``ctx["_secret_probe"]`` set. The probe
                # only sees the names, never the values.
                probe = ctx.get("_secret_probe")
                if probe is not None and hasattr(probe, "add"):
                    name = path[len("secret.") :] if path != "secret" else ""
                    if name:
                        probe.add(name)
                return m.group(0)  # leave the {{ ... }} block untouched
            cur: Any = ctx
            try:
                for part in path.split("."):
                    if not hasattr(cur, "__getitem__"):
                        raise KeyError(path)
                    cur = cur[part]
                return str(cur)
            except KeyError as e:
                if default is not None:
                    return default
                raise KeyError(f"unknown template variable: {path!r}") from e

        return _BLOCK.sub(repl, value)
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    return value


def _ctx_from_descriptor(desc: Descriptor) -> dict[str, Any]:
    out: dict[str, Any] = {
        "system": desc.system.model_dump(),
        "defaults": desc.defaults,
        "var": desc.variables,
    }
    if desc.install_root is not None:
        out["install_root"] = str(desc.install_root)
    out["secret"] = _SecretLookup()
    # Variables are also exposed as top-level tokens for ergonomics:
    # `{{ db_name }}` works as well as `{{ var.db_name }}`.
    out.update(desc.variables)
    return out


def _ctx_with_secrets() -> dict[str, Any]:
    """Build a template context that contains ONLY a fresh
    ``secret`` namespace.

    Used by the engine at step-execution time to resolve
    `{{ secret.x }}` references that were intentionally left
    unresolved at load time. We deliberately omit `system`,
    `var`, `defaults`, etc. — the engine already substituted
    those at load, and any remaining `{{ secret.x }}` token in
    a step's command is exactly what we want to resolve here.
    """
    return {"secret": _SecretLookup()}


class _SecretLookup(dict):
    """Adapter that turns `{{ secret.x }}` into a vault lookup.

    Inherits ``dict`` so the dotted-path renderer can pick it up
    via ``cur[part]`` and we don't have to special-case the
    renderer. The first access instantiates a per-key proxy;
    the proxy resolves on ``str()`` and writes the result back
    into this dict so the next render of the same descriptor
    reuses the cached value.
    """

    def __init__(self) -> None:
        super().__init__()  # empty dict
        self._resolved: dict[str, str] = {}

    def __missing__(self, name: str) -> _SecretValue:
        proxy = _SecretValue(self, name)
        # Cache the proxy in the dict so repeated renders don't
        # keep creating new proxies. The proxy resolves on str()
        # and updates the cache via _cache() below.
        self[name] = proxy
        return proxy

    def _cache(self, name: str, value: str) -> None:
        """Called by a proxy after it has resolved the secret.

        Stores the resolved value in ``self[name]`` so subsequent
        ``str(lookup[name])`` calls return without going through
        the vault again.
        """
        self._resolved[name] = value
        self[name] = value


class _SecretValue:
    """One-shot proxy for a single secret.

    Behaves as a mapping on the way down the dotted path (so the
    existing template renderer can walk into it), and as a string
    on the way back out (so the renderer's final ``str(cur)``
    returns the resolved value). The single-key convention is
    `{{ secret.<name> }}`; nesting (`{{ secret.a.b }}`) is
    rejected because the secrets store is flat by name.
    """

    __slots__ = ("_parent", "_name", "_resolved")

    def __init__(self, parent: _SecretLookup, name: str) -> None:
        self._parent = parent
        self._name = name
        self._resolved: str | None = None

    def __getitem__(self, key: str) -> _SecretValue:
        # Any further dotted access past the secret name is
        # rejected: we don't support nested secret names.
        raise KeyError(f"secret reference is not nestable: {self._name!r}")

    def __str__(self) -> str:
        if self._resolved is not None:
            return self._resolved
        # First access — ask the vault, then cache in the parent
        # so subsequent renders of the same descriptor don't
        # touch the vault again.
        value = _secrets.get_vault().resolve(self._name)
        self._resolved = value
        self._parent._cache(self._name, value)
        return value

    def __repr__(self) -> str:
        return f"_SecretValue({self._name!r})"

    def __iter__(self):  # pragma: no cover — defensive
        raise TypeError("secret reference is a scalar, not a mapping")

    def keys(self):  # pragma: no cover — defensive
        raise TypeError("secret reference is a scalar, not a mapping")


def load_descriptor(
    path: Path | str,
    base_ctx: dict[str, Any] | None = None,
    *,
    vault: Any | None = None,
) -> Descriptor:
    """Load YAML, apply secret + template substitution, validate as Descriptor.

    If ``vault`` is provided, ``{% secret "name" %}`` tokens are
    replaced before any other parsing so credentials never land in
    logs or process state in plaintext form.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: descriptor must be a YAML mapping at the root")

    if vault is not None:
        from orchx.secrets import substitute_secrets

        raw = substitute_secrets(raw, vault)

    # Two-pass: first build a partial model so we can use its system.* /
    # defaults.* in the second pass for templating.
    try:
        parsed = Descriptor.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"{path}: descriptor validation failed:\n{e}") from e

    ctx = _ctx_from_descriptor(parsed)
    if base_ctx:
        ctx.update(base_ctx)

    rendered = render_template(raw, ctx)
    if not isinstance(rendered, dict):
        raise ValueError("rendered descriptor lost its mapping shape")

    try:
        return Descriptor.model_validate(rendered)
    except ValidationError as e:
        raise ValueError(f"{path}: rendered descriptor failed validation:\n{e}") from e


def render_descriptor(
    desc: Descriptor,
    extra_ctx: dict[str, Any] | None = None,
) -> Descriptor:
    """Re-render a fully loaded Descriptor (e.g. with --set overrides)."""
    ctx = _ctx_from_descriptor(desc)
    if extra_ctx:
        ctx.update(extra_ctx)
    raw = desc.model_dump()
    rendered = render_template(raw, ctx)
    return Descriptor.model_validate(rendered)
