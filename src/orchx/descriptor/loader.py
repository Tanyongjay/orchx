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

from orchx.descriptor.models import Descriptor

_TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_\.]*)\s*\}\}")


def render_template(value: Any, ctx: dict[str, Any]) -> Any:
    """Recursively substitute ``{{ name.path }}`` inside ``value``."""
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            cur: Any = ctx
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    raise KeyError(f"unknown template variable: {key!r}")
                cur = cur[part]
            return str(cur)

        return _TOKEN.sub(repl, value)
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
    # Variables are also exposed as top-level tokens for ergonomics:
    # `{{ db_name }}` works as well as `{{ var.db_name }}`.
    out.update(desc.variables)
    return out


def load_descriptor(path: Path | str, base_ctx: dict[str, Any] | None = None) -> Descriptor:
    """Load YAML, apply template substitution, validate as Descriptor."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: descriptor must be a YAML mapping at the root")

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
