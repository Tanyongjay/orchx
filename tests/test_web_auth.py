"""Tests for the v0.3.0-alpha auth gate.

These tests are colocated in a separate file (rather than
appended to test_web_api.py) because the auth-mode is an
app-level config that doesn't compose cleanly with the
session-scoped `client` fixture in test_web_api.py. We build
a fresh TestClient per test here so each test can pass its
own AuthConfig.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchx.web.app import _make_app
from orchx.web.auth import (
    AUTH_MODES,
    AuthConfig,
    auth_config_from_env,
)


def _basic_config(user: str = "admin", password: str = "s3cr3t") -> AuthConfig:
    return AuthConfig(
        mode="basic",
        basic_user=user,
        basic_password_hash=hashlib.sha256(password.encode("utf-8")).hexdigest(),
    )


def _api_key_config(key: str = "my-token") -> AuthConfig:
    return AuthConfig(
        mode="api_key",
        api_key_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
    )


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub ORCHX_AUTH_* from the host shell so each test
    sees a deterministic env (the AuthConfig comes from
    the ``_basic_config`` / ``_api_key_config`` helpers,
    not from the process environment).
    """
    for k in list(os.environ):
        if k.startswith("ORCHX_AUTH_"):
            monkeypatch.delenv(k, raising=False)


def _basic_header(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {raw}"}


# ---- mode=none ----


def test_mode_none_allows_anonymous_access(tmp_path: Path) -> None:
    """The v0.2.x behaviour is preserved: auth_mode=none
    means the dashboard and the JSON API are open. This is
    the default so existing deployments don't break.
    """
    app = _make_app(db_path=tmp_path / "auth_none.sqlite")
    with TestClient(app) as c:
        r = c.get("/api/runs")
        assert r.status_code == 200
        # /api/auth still works and reports mode=none.
        status = c.get("/api/auth").json()
        assert status["mode"] == "none"
        assert status["requires_credentials"] is False


def test_healthz_is_always_open(tmp_path: Path) -> None:
    """/healthz is unauthenticated even in basic mode, so load
    balancers and uptime monitors can probe liveness without
    a credential.
    """
    app = _make_app(
        db_path=tmp_path / "healthz.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_api_auth_endpoint_is_always_open(tmp_path: Path) -> None:
    """The dashboard needs to ask "is auth required?" before
    showing a login screen. /api/auth must therefore be
    open in every auth mode.
    """
    app = _make_app(
        db_path=tmp_path / "auth_status.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        r = c.get("/api/auth")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "basic"
        assert body["username"] == "admin"
        assert body["requires_credentials"] is True


# ---- mode=basic ----


def test_basic_mode_rejects_unauthenticated_request(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "basic_no_creds.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        r = c.get("/api/runs")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
        assert "Basic" in r.headers["WWW-Authenticate"]
        assert r.json() == {"detail": "authentication required"}


def test_basic_mode_rejects_wrong_password(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "basic_wrong.sqlite",
        auth_config=_basic_config(user="admin", password="s3cr3t"),
    )
    with TestClient(app) as c:
        r = c.get(
            "/api/runs",
            headers=_basic_header("admin", "WRONG"),
        )
        assert r.status_code == 401


def test_basic_mode_rejects_wrong_user(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "basic_wrong_user.sqlite",
        auth_config=_basic_config(user="admin", password="s3cr3t"),
    )
    with TestClient(app) as c:
        r = c.get(
            "/api/runs",
            headers=_basic_header("someone-else", "s3cr3t"),
        )
        assert r.status_code == 401


def test_basic_mode_accepts_correct_creds(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "basic_good.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        r = c.get(
            "/api/runs",
            headers=_basic_header("admin", "s3cr3t"),
        )
        assert r.status_code == 200


def test_basic_mode_rejects_malformed_auth_header(tmp_path: Path) -> None:
    """A header that's not valid base64 or not 'Basic ...' must
    be rejected, not raise an exception.
    """
    app = _make_app(
        db_path=tmp_path / "basic_malformed.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        # No colon after base64 decode.
        r = c.get(
            "/api/runs",
            headers={"Authorization": "Basic " + base64.b64encode(b"nocolon").decode()},
        )
        assert r.status_code == 401
        # Not Basic.
        r = c.get(
            "/api/runs",
            headers={"Authorization": "Bearer abc"},
        )
        assert r.status_code == 401


# ---- mode=api_key ----


def test_api_key_mode_rejects_unauthenticated_request(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "key_no_creds.sqlite",
        auth_config=_api_key_config(),
    )
    with TestClient(app) as c:
        r = c.get("/api/runs")
        assert r.status_code == 401
        assert "Bearer" in r.headers["WWW-Authenticate"]


def test_api_key_mode_rejects_wrong_key(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "key_wrong.sqlite",
        auth_config=_api_key_config("my-token"),
    )
    with TestClient(app) as c:
        r = c.get("/api/runs", headers={"Authorization": "Bearer WRONG"})
        assert r.status_code == 401


def test_api_key_mode_accepts_correct_key(tmp_path: Path) -> None:
    app = _make_app(
        db_path=tmp_path / "key_good.sqlite",
        auth_config=_api_key_config("my-token"),
    )
    with TestClient(app) as c:
        r = c.get("/api/runs", headers={"Authorization": "Bearer my-token"})
        assert r.status_code == 200


def test_api_key_mode_accepts_query_token(tmp_path: Path) -> None:
    """The WebSocket endpoint needs an alternative to the
    Authorization header (browsers can't set headers on a
    WebSocket), so the middleware also accepts ?token=...
    """
    app = _make_app(
        db_path=tmp_path / "key_query.sqlite",
        auth_config=_api_key_config("my-token"),
    )
    with TestClient(app) as c:
        r = c.get("/api/runs?token=my-token")
        assert r.status_code == 200
        r = c.get("/api/runs?token=WRONG")
        assert r.status_code == 401


# ---- POST is also gated ----


def test_post_runs_is_gated_in_basic_mode(tmp_path: Path) -> None:
    """A POST that creates a new run must be gated. Without
    this, an unauthenticated caller could trigger deployments.
    """
    app = _make_app(
        db_path=tmp_path / "post_gate.sqlite",
        auth_config=_basic_config(),
    )
    with TestClient(app) as c:
        r = c.post(
            "/api/runs",
            json={
                "descriptor": "descriptors/sample_webapp_erp.yaml",
                "target": "mock://local",
            },
        )
        assert r.status_code == 401
        # With the right creds, the same POST succeeds.
        r = c.post(
            "/api/runs",
            json={
                "descriptor": "descriptors/sample_webapp_erp.yaml",
                "target": "mock://local",
            },
            headers=_basic_header("admin", "s3cr3t"),
        )
        assert r.status_code == 200


# ---- env loader ----


def test_auth_config_from_env_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ORCHX_AUTH_MODE is unset we land in mode=none so
    v0.2.x deployments keep working without env changes.
    """
    monkeypatch.delenv("ORCHX_AUTH_MODE", raising=False)
    cfg = auth_config_from_env()
    assert cfg.mode == "none"
    assert cfg.requires_credentials is False


def test_auth_config_from_env_basic_requires_user_and_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHX_AUTH_MODE", "basic")
    monkeypatch.setenv("ORCHX_AUTH_BASIC_USER", "admin")
    monkeypatch.setenv("ORCHX_AUTH_BASIC_PASSWORD", "s3cr3t")
    cfg = auth_config_from_env()
    assert cfg.mode == "basic"
    assert cfg.basic_user == "admin"
    assert cfg.basic_password_hash == hashlib.sha256(b"s3cr3t").hexdigest()


def test_auth_config_from_env_basic_rejects_missing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHX_AUTH_MODE", "basic")
    monkeypatch.setenv("ORCHX_AUTH_BASIC_USER", "admin")
    monkeypatch.delenv("ORCHX_AUTH_BASIC_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="ORCHX_AUTH_BASIC_PASSWORD"):
        auth_config_from_env()


def test_auth_config_from_env_api_key_requires_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHX_AUTH_MODE", "api_key")
    monkeypatch.setenv("ORCHX_AUTH_API_KEY", "my-token")
    cfg = auth_config_from_env()
    assert cfg.mode == "api_key"
    assert cfg.api_key_hash == hashlib.sha256(b"my-token").hexdigest()


def test_auth_config_from_env_rejects_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHX_AUTH_MODE", "wrong-mode")
    with pytest.raises(ValueError, match="must be one of"):
        auth_config_from_env()


def test_auth_modes_constant_lists_supported_modes() -> None:
    """The constant is exported so external tools (e.g. the
    installer) can introspect what's possible without
    reading the source.
    """
    assert set(AUTH_MODES) == {"none", "basic", "api_key"}
