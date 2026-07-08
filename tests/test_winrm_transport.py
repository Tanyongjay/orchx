"""Unit tests for the WinRM transport.

These tests cover:
  * URI parsing (no real network call).
  * The transport's error path when ``pywinrm`` is not installed.
  * Lazy import — accessing the module must not fail when
    ``pywinrm`` is absent.

They intentionally do NOT attempt a real WinRM connection; that
needs a live Windows host and is exercised by the integration
suite (orchx/tests/test_winrm_integration.py — out of scope here).
"""

from __future__ import annotations

import pytest

from orchx.transports import registry
from orchx.transports.winrm import WinRMTransport, WinRMTransportError, _parse_winrm_uri


def test_uri_https_default_port():
    creds = _parse_winrm_uri("winrm://alice:s3cr3t@10.0.0.1")
    assert creds.user == "alice"
    assert creds.password == "s3cr3t"
    assert creds.host == "10.0.0.1"
    assert creds.port == 5986
    assert creds.use_ssl is True
    assert creds.endpoint == "https://10.0.0.1:5986/wsman"


def test_uri_http_explicit_port():
    creds = _parse_winrm_uri("winrm-http://admin:%21@1.2.3.4:15985")
    assert creds.user == "admin"
    assert creds.password == "!"
    assert creds.host == "1.2.3.4"
    assert creds.port == 15985
    assert creds.use_ssl is False


def test_uri_with_percent_encoded_password():
    creds = _parse_winrm_uri("winrm://root:p%40ss%3Aword@10.0.0.5:5986")
    assert creds.password == "p@ss:word"


def test_uri_rejects_missing_user():
    with pytest.raises(WinRMTransportError, match="user:password"):
        _parse_winrm_uri("winrm://:pw@10.0.0.1")


def test_uri_rejects_unknown_scheme():
    with pytest.raises(WinRMTransportError, match="not a winrm URI"):
        _parse_winrm_uri("ssh://alice@host")


def test_registry_lists_winrm_schemes():
    assert "winrm" in registry._REGISTRY  # type: ignore[attr-defined]
    assert "winrm-http" in registry._REGISTRY  # type: ignore[attr-defined]


def test_constructor_raises_when_pywinrm_missing(monkeypatch):
    """If pywinrm is not importable, the constructor should fail loud."""
    import orchx.transports.winrm as winrm_mod

    # Force ImportError on the pywinrm import inside __init__.
    def boom(_name: str):
        raise ImportError("simulated missing pywinrm")

    monkeypatch.setattr(winrm_mod, "winrm", None, raising=False)
    # Replace builtins.__import__ for the 'winrm' module name.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "winrm" or name.startswith("winrm."):
            raise ImportError("simulated missing pywinrm")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(WinRMTransportError, match="pywinrm is required"):
        WinRMTransport("winrm://alice:pw@10.0.0.1")


def test_parse_uri_does_not_require_pywinrm():
    """URI parsing must work even when pywinrm is not installed."""
    # _parse_winrm_uri has no top-level pywinrm import — this must
    # succeed even on a box that never installed the real transport.
    creds = _parse_winrm_uri("winrm://a:b@10.0.0.1")
    assert creds.user == "a"
    assert creds.password == "b"
