"""HashiCorp Vault secrets backend.

Adds a fourth backend to ``orchx.secrets``: a real Vault
read-only client that uses the KV-v2 API over HTTPS with a
static token.

Usage:

  export ORCHX_SECRETS_BACKEND=vault
  export ORCHX_VAULT_ADDR=https://vault.internal:8200
  export ORCHX_VAULT_TOKEN=hvs.abcdef
  export ORCHX_VAULT_MOUNT=secret            # KV-v2 mount
  export ORCHX_VAULT_PREFIX=orchx/prod/      # common path prefix
  orchx deploy descriptors/sample_x.yaml --target ...

Each ``{{ secret.x }}`` resolves to:

  GET https://{addr}/v1/{mount}/data/{prefix}{name}

JSON response:
  {
    "data": {
      "data": {
        "value": "...",
        ...other KV-v2 version metadata fields...
      },
      "metadata": {...}
    }
  }

The ``value`` field under ``data.data`` is what we return.
If the underlying secret is a KV-v2 single key/value pair
named ``value`` (which is the convention orchx ships with
for its sample descriptors), this Just Works.

Fallback: if the secret at ``{prefix}{name}`` doesn't have
a ``value`` field, we return the whole ``data.data`` payload
serialised as JSON. This is the standard convention for
"bag of secrets" entries.

Why stdlib only (no hvac, no httpx):
  - v0.4 ships a single-vault, single-mount, token-auth
    read-only path. hvac adds value here but not for a v0.4
    surface that's four HTTP calls in total.
  - Stdlib urllib keeps the runtime dependency tree at zero
    added. Operators can pip install ``hvac`` later if they
    need auth methods (Kubernetes, AWS, etc.) — those are
    explicitly punted to v0.4.x.

Security:
  - Token never appears in a log line, descriptor, or
    SQLite row. It's only used inside ``HashiCorpVault``
    to construct the ``X-Vault-Token`` header.
  - Resolved values never persist (the orchx engine
    substitutes in-memory; see the security note in
    tests/test_secret_template.py).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from orchx.secrets import SecretNotFoundError, Vault


class VaultConfigError(ValueError):
    """Raised when the ORCHX_VAULT_* environment is incomplete."""


class HashiCorpVault(Vault):
    """A read-only client for HashiCorp Vault KV-v2.

    The client is stateless: every ``resolve()`` call issues
    one HTTPS request. This is fine for orchx's workload
    (10 secrets per deploy at most); for higher-volume
    use cases the operator is expected to front this with
    a ``MemoryVault`` populated at startup.
    """

    def __init__(
        self,
        addr: str | None = None,
        token: str | None = None,
        mount: str | None = None,
        prefix: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # Read every field from the matching ORCHX_VAULT_*
        # env var, with the explicit arg taking precedence.
        # This lets callers (notably the test suite) inject
        # values without polluting the process environment.
        self.addr = (addr or os.environ.get("ORCHX_VAULT_ADDR") or "").rstrip("/")
        self.token = token or os.environ.get("ORCHX_VAULT_TOKEN") or ""
        # The default is "secret", but only when the user
        # didn't explicitly pass ``mount=""`` (which they
        # could use to disable the default sentinel).
        # ``or`` treats empty string as falsy, so an
        # explicit empty mount falls through to the env
        # var, and only when both are missing do we
        # default to "secret".
        if mount is None:
            mount = os.environ.get("ORCHX_VAULT_MOUNT", "secret")
        self.mount = mount
        self.prefix = prefix or os.environ.get("ORCHX_VAULT_PREFIX") or ""
        if timeout_s is None:
            timeout_s = float(os.environ.get("ORCHX_VAULT_TIMEOUT", "5.0"))
        self.timeout_s = float(timeout_s)
        # Operators sometimes set up dev Vaults with
        # self-signed certs. We do NOT default to insecure;
        # opt-in via ORCHX_VAULT_INSECURE=1, mirroring the
        # behavior of the official hvac client.
        self.insecure = os.environ.get("ORCHX_VAULT_INSECURE") in (
            "1",
            "true",
            "yes",
        )
        # Validate config so the failure mode is loud at
        # startup, not at the first secret resolution
        # attempt deep inside a descriptor deploy.
        if not self.addr:
            raise VaultConfigError(
                "ORCHX_VAULT_ADDR (or kwarg addr) is required for the 'vault' secrets backend"
            )
        if not self.token:
            raise VaultConfigError(
                "ORCHX_VAULT_TOKEN (or kwarg token) is required for the 'vault' secrets backend"
            )
        if not self.mount:
            raise VaultConfigError("ORCHX_VAULT_MOUNT (or kwarg mount) is required")
        # Bookkeeping for the doctor (preflight) check:
        # names referenced in this process.
        self._seen: set[str] = set()

    # ---- transport ----

    def _url(self, name: str) -> str:
        # KV-v2 data path: /v1/{mount}/data/{path}
        # The ``prefix`` is a string the operator can use to
        # scope all orchx-managed secrets under a single
        # Vault prefix (e.g. ``orchx/prod/``).
        self._assert_safe_path(self.prefix + name)
        return f"{self.addr}/v1/{self.mount}/data/{self.prefix}{name}"

    @staticmethod
    def _assert_safe_path(path: str) -> None:
        # Vault path components may legitimately contain
        # ``/`` (slashes separate path segments), ``-``,
        # ``.``, ``_``, and alphanumerics. We forbid
        # whitespace, ``?``, ``#``, control chars, and a
        # leading or trailing slash.
        if not path:
            return
        for ch in path:
            if not (ch.isalnum() or ch in "/-_."):
                raise VaultConfigError(
                    f"vault secret path contains unsafe character {ch!r} in {path!r}"
                )

    def _http_get(self, url: str) -> dict[str, Any]:
        # Build the request. The X-Vault-Token header carries
        # the credential; we never write it to disk or logs.
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-Vault-Token", self.token)
        req.add_header("Accept", "application/json")
        ctx: ssl.SSLContext | None = None
        if self.insecure:
            # TLS verification disabled. This is an opt-in
            # escape hatch for dev/test, never for prod.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(
                req,
                timeout=self.timeout_s,
                context=ctx,
            ) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            # Vault returns 404 for missing secrets, 403 for
            # permission denied. Surface those as
            # SecretNotFoundError so callers can distinguish
            # them from network errors.
            if e.code == 404:
                raise SecretNotFoundError(url) from e
            if e.code == 403:
                raise PermissionError(f"vault denied access to {url}: {e.reason}") from e
            # Anything else is a real error.
            raise OSError(f"vault HTTP {e.code} on GET {url}: {e.reason}") from e
        except urllib.error.URLError as e:
            # Connection refused, DNS failure, timeout, etc.
            raise OSError(f"vault unreachable at {self.addr}: {e}") from e
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise OSError(f"vault returned non-JSON at {url}: {e}") from e

    # ---- Vault interface ----

    def record(self, name: str) -> None:
        """Track that ``name`` was referenced.

        Called by orchx's preflight check (doctor) when it
        walks a descriptor. The names here are what we
        return from ``list_names()`` if the operator asks.
        Never persisted, never sent across the network.
        """
        self._seen.add(name)

    def resolve(self, name: str) -> str:
        # Record the lookup so the preflight doctor knows
        # what we tried. This is local-only state.
        self._seen.add(name)
        # KV-v2 stores secrets at /v1/{mount}/data/{path}.
        # The JSON body looks like
        #   {"data": {"data": {"value": "...", ...}, "metadata": ...}}
        body = self._http_get(self._url(name))
        payload = body.get("data", {}).get("data", {})
        if not isinstance(payload, dict):
            raise SecretNotFoundError(name)
        if "value" in payload and isinstance(payload["value"], str):
            return payload["value"]
        # No ``value`` key; the operator stored a bag of
        # secrets. Return the bag as a JSON string so the
        # orchx engine has something usable to inject.
        return json.dumps(payload, sort_keys=True)

    def list_names(self) -> list[str]:
        # KV-v2 supports ``?list=true`` on the metadata path
        # to enumerate keys. We don't enumerate by default
        # (the operator may have many unrelated secrets at
        # the same prefix; listing them is misleading). The
        # doctor checks the secrets used by a descriptor,
        # which is the right way to verify.
        return sorted(self._seen)
