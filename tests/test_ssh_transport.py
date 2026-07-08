"""Unit tests for the SSH transport.

These cover:
  * URI parsing (no real network call).
  * The transport's error path when ``asyncssh`` is not installed.
  * Lazy import — accessing the module must not fail when
    ``asyncssh`` is absent.

They intentionally do NOT attempt a real SSH connection; that
needs a live Linux host and is exercised by the integration
suite (out of scope here).
"""

from __future__ import annotations

import builtins

import pytest

from orchx.transports import registry
from orchx.transports.ssh import SSHTransport, SSHTransportError, _parse_ssh_uri


def test_uri_password_default_port():
    creds = _parse_ssh_uri("ssh://alice:s3cr3t@10.0.0.1")
    assert creds.user == "alice"
    assert creds.password == "s3cr3t"
    assert creds.host == "10.0.0.1"
    assert creds.port == 22
    assert creds.keyfile is None
    assert creds.endpoint == "alice@10.0.0.1:22"


def test_uri_explicit_port():
    creds = _parse_ssh_uri("ssh://root:pw@10.0.0.1:2222")
    assert creds.port == 2222


def test_uri_keyfile_query():
    creds = _parse_ssh_uri("ssh+key://deploy@10.0.0.5:22?keyfile=/etc/orchx/id_rsa")
    assert creds.user == "deploy"
    assert creds.password is None
    assert creds.keyfile == "/etc/orchx/id_rsa"
    assert creds.passphrase is None


def test_uri_keyfile_with_passphrase():
    creds = _parse_ssh_uri("ssh+key://u@h:22?keyfile=/k&passphrase=secret%21")
    assert creds.keyfile == "/k"
    assert creds.passphrase == "secret!"


def test_uri_rejects_missing_user():
    with pytest.raises(SSHTransportError, match="user@"):
        _parse_ssh_uri("ssh://h:22")


def test_uri_rejects_unknown_scheme():
    with pytest.raises(SSHTransportError, match="not an ssh URI"):
        _parse_ssh_uri("telnet://user@h:23")


def test_uri_ssh_plus_key_requires_keyfile():
    with pytest.raises(SSHTransportError, match="keyfile"):
        _parse_ssh_uri("ssh+key://user@h:22")


def test_registry_lists_ssh_schemes():
    assert "ssh" in registry._REGISTRY  # type: ignore[attr-defined]
    assert "ssh+key" in registry._REGISTRY  # type: ignore[attr-defined]


def test_constructor_raises_when_asyncssh_missing(monkeypatch):
    """If asyncssh is not importable, the constructor should fail loud."""
    import orchx.transports.ssh as ssh_mod

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("simulated missing asyncssh")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Also drop any cached reference to a successful import.
    monkeypatch.delattr(ssh_mod, "asyncssh", raising=False)

    with pytest.raises(SSHTransportError, match="asyncssh is required"):
        SSHTransport("ssh://alice:pw@10.0.0.1")


def test_parse_uri_does_not_require_asyncssh():
    """URI parsing must work even when asyncssh is not installed."""
    creds = _parse_ssh_uri("ssh://a:b@10.0.0.1")
    assert creds.user == "a"
    assert creds.password == "b"


def test_get_transport_for_ssh_uri_blocks_when_asyncssh_missing(monkeypatch):
    """Registry must surface a clean error when asyncssh is absent."""
    import orchx.transports.ssh as ssh_mod

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delattr(ssh_mod, "asyncssh", raising=False)

    with pytest.raises(SSHTransportError, match="asyncssh is required"):
        registry.get_transport("ssh://a:b@10.0.0.1")
