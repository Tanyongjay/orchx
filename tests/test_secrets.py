"""Tests for the secrets module."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchx.secrets import (
    EnvVault,
    FileVault,
    MemoryVault,
    SecretNotFoundError,
    get_vault,
    register_vault,
    substitute_secrets,
)

# ---------- EnvVault ----------


@pytest.fixture(autouse=True)
def clean_secrets_env(monkeypatch):
    """Scrub ORCHX_SECRET_* from the host shell so each test sees
    a deterministic empty env and never accidentally reads a
    real secret from the developer's machine.
    """
    for k in list(os.environ):
        if k.startswith("ORCHX_SECRET_") or k == "ORCHX_SECRETS_BACKEND":
            monkeypatch.delenv(k, raising=False)


def test_env_vault_resolve_and_list(monkeypatch):
    monkeypatch.setenv("ORCHX_SECRET_USER", "alice")
    monkeypatch.setenv("ORCHX_SECRET_PASS", "hunter2")
    v = EnvVault()
    assert v.resolve("USER") == "alice"
    assert v.resolve("PASS") == "hunter2"
    assert set(v.list_names()) == {"USER", "PASS"}


def test_env_vault_missing_raises(monkeypatch):
    monkeypatch.setenv("ORCHX_SECRET_NOPE", "x")
    monkeypatch.delenv("ORCHX_SECRET_NOPE", raising=False)
    v = EnvVault()
    with pytest.raises(SecretNotFoundError):
        v.resolve("NOPE")


# ---------- FileVault ----------


def test_file_vault_json(tmp_path: Path):
    p = tmp_path / "secrets.json"
    p.write_text('{"a": "1", "b": "2"}')
    v = FileVault(p)
    assert v.resolve("a") == "1"
    assert v.resolve("b") == "2"
    assert v.list_names() == ["a", "b"]


def test_file_vault_yaml(tmp_path: Path):
    p = tmp_path / "secrets.yaml"
    p.write_text('a: "1"\nb: "2"\n')
    v = FileVault(p)
    assert v.resolve("a") == "1"
    assert v.resolve("b") == "2"


def test_file_vault_rejects_non_string_values(tmp_path: Path):
    p = tmp_path / "secrets.json"
    p.write_text('{"a": 1}')
    with pytest.raises(ValueError, match="must be a string"):
        FileVault(p)


def test_file_vault_rejects_non_mapping(tmp_path: Path):
    p = tmp_path / "secrets.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="must be a mapping"):
        FileVault(p)


# ---------- MemoryVault ----------


def test_memory_vault_set_and_resolve():
    v = MemoryVault({"a": "1"})
    v.set("b", "2")
    assert v.resolve("a") == "1"
    assert v.resolve("b") == "2"
    with pytest.raises(SecretNotFoundError):
        v.resolve("c")


# ---------- get_vault ----------


def test_get_vault_default_is_env(monkeypatch):
    monkeypatch.delenv("ORCHX_SECRETS_BACKEND", raising=False)
    monkeypatch.setenv("ORCHX_SECRET_X", "y")
    v = get_vault()
    assert isinstance(v, EnvVault)
    assert v.resolve("X") == "y"


def test_get_vault_with_explicit_name(monkeypatch):
    monkeypatch.setenv("ORCHX_SECRET_X", "y")
    v = get_vault("memory", secrets={"X": "z"})
    assert isinstance(v, MemoryVault)
    assert v.resolve("X") == "z"


def test_get_vault_file_with_path(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text('{"k": "v"}')
    v = get_vault("file", path=p)
    assert isinstance(v, FileVault)
    assert v.resolve("k") == "v"


def test_get_vault_unknown_raises():
    with pytest.raises(ValueError, match="unknown secrets backend"):
        get_vault("nonexistent")


def test_register_vault_custom():
    class MyVault:
        def __init__(self, **kw):
            self.kw = kw

        def resolve(self, n):
            return "fixed"

        def list_names(self):
            return ["*"]

    register_vault("test-custom", MyVault)
    v = get_vault("test-custom", foo="bar")
    assert v.resolve("anything") == "fixed"


# ---------- substitute_secrets ----------


def test_substitute_secrets_in_string():
    v = MemoryVault({"USER": "alice", "PASS": "p@ss"})
    assert (
        substitute_secrets('winrm://{% secret "USER" %}:{% secret "PASS" %}@h', v)
        == "winrm://alice:p@ss@h"
    )


def test_substitute_secrets_recursive_dict():
    v = MemoryVault({"PASS": "p@ss"})
    out = substitute_secrets(
        {"target": 'ssh://u:{% secret "PASS" %}@h', "x": 1},
        v,
    )
    assert out == {"target": "ssh://u:p@ss@h", "x": 1}


def test_substitute_secrets_recursive_list():
    v = MemoryVault({"A": "x", "B": "y"})
    out = substitute_secrets(['{% secret "A" %}', '{% secret "B" %}'], v)
    assert out == ["x", "y"]


def test_substitute_secrets_missing_raises():
    v = MemoryVault({})
    with pytest.raises(ValueError, match="not found"):
        substitute_secrets('{% secret "missing" %}', v)


def test_substitute_secrets_passes_through_non_string():
    v = MemoryVault({})
    assert substitute_secrets(42, v) == 42
    assert substitute_secrets(None, v) is None
    assert substitute_secrets(True, v) is True
