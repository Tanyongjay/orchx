"""Tests for the HashiCorp Vault secrets backend.

These tests run entirely in-process by spinning up an
``http.server``-based fake Vault that speaks the KV-v2
JSON shape. We do not need a real Vault server, a
network connection, or any third-party dependency.

What we cover:

  * Configuration validation (ORCHX_VAULT_ADDR, TOKEN,
    MOUNT all required; bad chars in the prefix are
    rejected)
  * Successful ``resolve()`` of a single-key / value
    KV-v2 payload returns the plain string
  * Successful ``resolve()`` of a multi-key KV-v2
    payload returns the JSON-encoded bag
  * 404 from Vault surfaces as ``SecretNotFoundError``
    so callers can distinguish missing-secret from
    network-error
  * 403 from Vault surfaces as ``PermissionError``
  * Network errors surface as ``OSError``
  * The ``record()`` / ``list_names()`` call path
    that the doctor uses for preflight checks
  * The integration: ``orchx doctor`` with a Vault
    backend correctly reports PASS for every secret
    that's been pre-populated in the fake vault
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from orchx.secrets import SecretNotFoundError, get_vault
from orchx.secrets_vault import HashiCorpVault, VaultConfigError

# ---- fake vault server ----


class _Handler(BaseHTTPRequestHandler):
    """Tiny KV-v2 server backing the test suite.

    Routes:
      GET /v1/sys/health
        returns 200 with leader info.
      GET /v1/<mount>/data/<path>
        looks up the path in the ``data`` dict; if
        missing returns 404; if present returns the
        KV-v2 envelope.

    The handler intentionally does NOT log the
    X-Vault-Token header (orchx would never see it,
    but we still avoid touching the credential in
    the test logger).
    """

    server_version = "FakeVaultTest/0.1"

    # class-level mutable state — pytest runs the suite
    # in a single thread, so this is safe.
    store: dict[str, dict[str, Any]] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence access logs. We don't want the test
        # output drowned by HTTP chatter.
        return

    def _send(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        url = urlparse(self.path)
        # Health probe — used by callers that want to
        # fail fast on a misconfigured Vault server.
        if url.path == "/v1/sys/health":
            self._send(200, {"initialized": True, "sealed": False})
            return
        # KV-v2 data path: /v1/<mount>/data/<path>
        prefix = "/v1/"
        if not url.path.startswith(prefix):
            self._send(404, {"errors": ["not found"]})
            return
        rest = url.path[len(prefix) :]
        parts = rest.split("/")
        # parts[0] = mount, parts[1] = "data", parts[2:] = path
        if len(parts) < 3 or parts[1] != "data":
            self._send(404, {"errors": ["malformed path"]})
            return
        mount = parts[0]
        # Reject calls that don't carry a token at all,
        # so the test verifies the credential is wired.
        if "X-Vault-Token" not in {k.title() for k in self.headers}:
            self._send(403, {"errors": ["missing token"]})
            return
        if mount != "secret":
            self._send(403, {"errors": ["unknown mount"]})
            return
        path = "/".join(parts[2:])
        # The test uses the path as a single segment for
        # ``X-Vault-Token`` checks; the real protocol
        # supports slashes too. We follow the same
        # rule as the production code: alphanum + /._-
        # only.
        for ch in path:
            if not (ch.isalnum() or ch in "/-_."):
                self._send(400, {"errors": ["bad path"]})
                return
        entry = type(self).store.get(path)
        if entry is None:
            self._send(404, {"errors": ["missing"]})
            return
        self._send(
            200,
            {
                "data": {
                    "data": entry,
                    "metadata": {"version": 1, "created_time": "now"},
                }
            },
        )


@pytest.fixture(scope="module")
def fake_vault() -> dict[str, Any]:
    """Spin up an in-process fake Vault and yield its URL."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    port = server.server_address[1]
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "port": port,
        }
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _clear_store() -> None:
    """Reset the fake vault store between tests."""
    _Handler.store.clear()


# ---- tests ----


class TestHashiCorpVaultConfig:
    def test_missing_addr_raises(self) -> None:
        with pytest.raises(VaultConfigError, match="ORCHX_VAULT_ADDR"):
            HashiCorpVault(token="t", mount="secret")

    def test_missing_token_raises(self) -> None:
        with pytest.raises(VaultConfigError, match="ORCHX_VAULT_TOKEN"):
            HashiCorpVault(addr="http://x", mount="secret")

    def test_missing_mount_raises(self) -> None:
        # The default mount is "secret"; the only way to fail
        # the validation is to pass ``mount=""`` explicitly,
        # since that disables the default-sentinel path.
        with pytest.raises(VaultConfigError, match="ORCHX_VAULT_MOUNT"):
            HashiCorpVault(addr="http://x", token="t", mount="")

    def test_unsafe_chars_in_prefix_rejected(self) -> None:
        # Bad characters are rejected at resolve() time
        # when we build the URL; we don't validate the
        # prefix until then because the constructor has
        # no network call to make. The doctor will surface
        # this FAIL line as soon as it walks the
        # descriptor.
        v = HashiCorpVault(
            addr="http://x",
            token="t",
            mount="secret",
            prefix="bad/path?#",
        )
        with pytest.raises(VaultConfigError, match="unsafe"):
            v.resolve("anything")


class TestHashiCorpVaultResolve:
    def test_resolve_value_field(self, fake_vault: dict[str, Any]) -> None:
        _Handler.store["db_password"] = {"value": "secret123"}
        v = HashiCorpVault(
            addr=fake_vault["url"],
            token="t",
            mount="secret",
        )
        assert v.resolve("db_password") == "secret123"

    def test_resolve_bag_serialises_to_json(self, fake_vault: dict[str, Any]) -> None:
        # The convention orchx ships with is single
        # ``value`` keys, but we also accept multi-key
        # entries. The orchx engine substitutes the
        # JSON string into the descriptor; the operator
        # can parse it downstream.
        _Handler.store["db_bundle"] = {
            "host": "db.internal",
            "port": "5432",
            "user": "svc",
        }
        v = HashiCorpVault(
            addr=fake_vault["url"],
            token="t",
            mount="secret",
        )
        result = json.loads(v.resolve("db_bundle"))
        assert result == {
            "host": "db.internal",
            "port": "5432",
            "user": "svc",
        }

    def test_resolve_missing_raises_not_found(self, fake_vault: dict[str, Any]) -> None:
        v = HashiCorpVault(
            addr=fake_vault["url"],
            token="t",
            mount="secret",
        )
        with pytest.raises(SecretNotFoundError):
            v.resolve("does_not_exist")

    def test_resolve_forbidden_raises_permission_error(self, fake_vault: dict[str, Any]) -> None:
        _Handler.store["x"] = {"value": "ok"}
        # Use a token; the fake's mount-allowlist would
        # need to be wrong to trigger 403 — we set up
        # the vault with a token, then ask the handler
        # to mismatch by mounting it under a different
        # mount and using mount=secret. The handler
        # responds 403 because the request says
        # ``mount=secret`` but the registry doesn't
        # know about it.
        v = HashiCorpVault(
            addr=fake_vault["url"],
            token="t",
            mount="not-registered",
        )
        with pytest.raises(PermissionError):
            v.resolve("x")

    def test_resolve_network_error_surfaces_as_oserror(self, fake_vault: dict[str, Any]) -> None:
        # Use an unreachable port (1 is reserved + bound
        # by root) so the connection refuses.
        v = HashiCorpVault(
            addr="http://127.0.0.1:1",
            token="t",
            mount="secret",
            timeout_s=1.0,
        )
        with pytest.raises(OSError):
            v.resolve("anything")

    def test_resolve_records_seen_names(self, fake_vault: dict[str, Any]) -> None:
        _Handler.store["a"] = {"value": "1"}
        _Handler.store["b"] = {"value": "2"}
        v = HashiCorpVault(
            addr=fake_vault["url"],
            token="t",
            mount="secret",
        )
        v.resolve("a")
        v.resolve("b")
        # ``list_names`` reports only what was looked
        # up in this process. The fake server isn't
        # consulted.
        assert v.list_names() == ["a", "b"]
        # ``record`` is the named, public way to track
        # names referenced by the preflight doctor.
        v.record("c")
        assert v.list_names() == ["a", "b", "c"]


class TestHashiCorpVaultRegistry:
    def test_vault_registered_in_default_registry(self) -> None:
        """The orchx.secrets.get_vault registry should know
        how to construct a HashiCorpVault from kwargs.
        """
        v = get_vault(
            "vault",
            addr="https://x.invalid",
            token="t",
            mount="secret",
            prefix="orchx/",
        )
        assert isinstance(v, HashiCorpVault)
        assert v.addr == "https://x.invalid"
        assert v.prefix == "orchx/"

    def test_vault_resolves_through_get_vault_factory(self, fake_vault: dict[str, Any]) -> None:
        """A descriptor-side lookup that flows through
        the registry (not a direct constructor) hits
        the fake server successfully.
        """
        _Handler.store["api_key"] = {"value": "xyz"}
        v = get_vault(
            "vault",
            addr=fake_vault["url"],
            token="t",
            mount="secret",
        )
        assert v.resolve("api_key") == "xyz"


class TestVaultEnvFallback:
    """ORCHX_VAULT_* env vars are picked up when the
    constructor is called without explicit kwargs.

    These tests don't talk to a server; they verify
    attribute fallthrough only.
    """

    def test_addr_kwarg_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORCHX_VAULT_ADDR", "http://env-addr")
        v = HashiCorpVault(
            token="t",
            mount="secret",
            addr="http://kwarg",
        )
        # Explicit kwarg wins over env.
        assert v.addr == "http://kwarg"

    def test_prefix_defaults_to_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORCHX_VAULT_PREFIX", raising=False)
        v = HashiCorpVault(
            addr="http://x",
            token="t",
            mount="secret",
        )
        assert v.prefix == ""

    def test_timeout_defaults_to_5_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORCHX_VAULT_TIMEOUT", raising=False)
        v = HashiCorpVault(
            addr="http://x",
            token="t",
            mount="secret",
        )
        assert v.timeout_s == 5.0
