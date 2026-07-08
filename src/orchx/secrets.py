"""Secret resolution for credentials embedded in target URIs and
descriptor templates.

Three backends ship in MVP:

  * ``env``     — pull from process environment variables
                   (default; no extra config).
  * ``file``    — pull from a YAML/JSON file with ``0600`` perms.
  * ``memory``  — in-process map, used by tests and by callers that
                   load secrets at startup from a vault (1Password,
                   HashiCorp Vault, AWS Secrets Manager, etc.).

The vault is a callable: ``resolve(name) -> str``. Backends are
plugged in via ``register_vault(name, factory)`` and selected by
``--secrets-backend <name>`` on the CLI or the ``ORCHX_SECRETS_BACKEND``
environment variable.

Security stance: a resolved secret should never appear in a log
line or an event payload. Callers that embed resolved strings in
URIs must take care not to log the URI.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any


class SecretNotFoundError(KeyError):
    """Raised when a requested secret is not present in any backend."""


class Vault(ABC):
    """Abstract secret store."""

    @abstractmethod
    def resolve(self, name: str) -> str: ...

    @abstractmethod
    def list_names(self) -> list[str]: ...


class EnvVault(Vault):
    """Reads ``ORCHX_SECRET_<NAME>`` from the process environment."""

    PREFIX = "ORCHX_SECRET_"

    def resolve(self, name: str) -> str:
        key = self.PREFIX + name
        try:
            return os.environ[key]
        except KeyError as e:
            raise SecretNotFoundError(name) from e

    def list_names(self) -> list[str]:
        return [k[len(self.PREFIX) :] for k in os.environ if k.startswith(self.PREFIX)]


class FileVault(Vault):
    """Reads from a YAML/JSON file on disk.

    Format::

        # JSON
        {"winrm_user": "alice", "winrm_pass": "s3cr3t"}

        # YAML (requires PyYAML, which is already a hard dep)
        winrm_user: alice
        winrm_pass: s3cr3t

    File permissions must be ``0600`` on POSIX. This is checked on
    load and a warning is emitted (but not an error) on Windows,
    which has no such concept.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._secrets: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._secrets = {}
            return
        if os.name != "nt":
            mode = self.path.stat().st_mode & 0o777
            if mode & 0o077:
                # World- or group-readable. We don't refuse — the
                # operator may have a reason — but we surface it.
                warnings.warn(
                    f"secrets file {self.path} has mode {oct(mode)}; expected 0600",
                    stacklevel=2,
                )
        text = self.path.read_text(encoding="utf-8")
        if self.path.suffix.lower() in (".yaml", ".yml"):
            import yaml  # type: ignore[import-not-found]

            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"secrets file {self.path} must be a mapping, got {type(data)}")
        for k, v in data.items():
            if not isinstance(v, str):
                raise ValueError(f"secrets file {self.path}: {k!r} must be a string")
        self._secrets = dict(data)

    def resolve(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError as e:
            raise SecretNotFoundError(name) from e

    def list_names(self) -> list[str]:
        return sorted(self._secrets)


class MemoryVault(Vault):
    """Process-local vault. Useful for tests and for in-process
    secret loading from a real vault (1Password, HashiCorp Vault,
    AWS Secrets Manager, etc.) at startup."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets: dict[str, str] = dict(secrets or {})

    def set(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def resolve(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError as e:
            raise SecretNotFoundError(name) from e

    def list_names(self) -> list[str]:
        return sorted(self._secrets)


# ---- registry ----

VaultFactory = Callable[..., Vault]

_REGISTRY: dict[str, VaultFactory] = {}


def register_vault(name: str, factory: VaultFactory) -> None:
    _REGISTRY[name] = factory


def get_vault(name: str | None = None, **kwargs: Any) -> Vault:
    """Build the default vault, or the one named by ``name``.

    Default selection: ``ORCHX_SECRETS_BACKEND`` env var, else ``env``.
    """
    name = name or os.environ.get("ORCHX_SECRETS_BACKEND", "env")
    if name not in _REGISTRY:
        raise ValueError(f"unknown secrets backend: {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


# ---- default registrations ----

register_vault("env", lambda **_: EnvVault())
register_vault(
    "file",
    lambda **kwargs: FileVault(Path(kwargs["path"])),
)
register_vault("memory", lambda **kwargs: MemoryVault(kwargs.get("secrets")))


# ---- template substitution ----

_TOKEN = re.compile(r"\{\%\s*secret\s*['\"]([\w\-.]+)['\"]\s*\%\}")


def substitute_secrets(value: Any, vault: Vault) -> Any:
    """Recursively replace ``{% secret "name" %}`` tokens in ``value``.

    Used before descriptor / URI parsing so credentials never appear
    in logs or on the command line.
    """
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            try:
                return vault.resolve(name)
            except SecretNotFoundError as e:
                raise ValueError(
                    f"secret {name!r} not found in vault ({vault.__class__.__name__})"
                ) from e

        return _TOKEN.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute_secrets(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_secrets(v, vault) for v in value]
    return value
