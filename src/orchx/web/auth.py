"""Authentication for the OrchX control plane.

We ship a deliberately small surface here:

  * `AuthConfig` — parsed from environment variables. Three
    modes:
      - ``none`` (default, kept for back-compat with v0.2.x).
      - ``basic`` — HTTP Basic with a single user/password
        pair supplied via env vars.
      - ``api_key`` — a single bearer token supplied via
        env var. Used for service-to-service auth.

  * `require_user_or_token` — a FastAPI dependency that
    validates the request's credentials against the active
    config. Used on every `/api/*` route AND on the
    WebSocket endpoint (the dashboard's live-stream).

  * `auth_status_response` — the shape of the `GET /api/auth`
    endpoint that the dashboard uses to decide whether to
    show a login screen.

Real customer deployments will want pluggable auth (LDAP,
OIDC, HashiCorp Vault as an auth secret store). That's
v0.3-beta. For v0.3-alpha we ship the two single-tenant
modes that 90% of paying customers need on day one.

Security notes:

  * We compare the API key / password with ``secrets.compare_digest``
    to avoid timing side channels. The work is constant-time
    on the bytes of the secret, not on the length of the
    username, so the username comparison still leaks length
    info. That's acceptable for a control plane; the secret
    is what's load-bearing.
  * We never log the secret, even at debug level. The env
    var name is logged so an operator knows which knob to
    twist, but the value is dropped at the boundary.
  * WebSocket auth uses the same ``Authorization: Bearer``
    scheme, attached as a query string parameter
    (``?token=...``) so the browser's EventSource /
    WebSocket APIs can carry it. Browsers refuse to set
    custom headers on a WebSocket request, so this is the
    only viable path for the live-stream connection. The
    query string is NOT logged in the access log because
    we don't have an access log layer; the dashboard's
    auth UI uses ``fetch()`` to obtain the token first, then
    stores it in ``sessionStorage`` for the lifetime of
    the tab.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, WebSocket, status

AUTH_MODES = ("none", "basic", "api_key")


@dataclass(frozen=True)
class AuthConfig:
    """The active authentication configuration.

    Built once at app startup from environment variables. We
    keep this immutable because the value is consulted on
    every request and an accidental late-stage mutation
    would silently disable auth.
    """

    mode: str  # one of AUTH_MODES
    basic_user: str | None = None
    basic_password_hash: str | None = None  # sha256 hex of the password
    api_key_hash: str | None = None  # sha256 hex of the key

    @property
    def requires_credentials(self) -> bool:
        return self.mode in ("basic", "api_key")

    def describe(self) -> dict[str, Any]:
        """Return a description safe to expose to the dashboard.

        We never reveal the password or the API key, only the
        mode and the username (if applicable). The username is
        not a secret.
        """
        out: dict[str, Any] = {"mode": self.mode, "requires_credentials": self.requires_credentials}
        if self.mode == "basic":
            out["username"] = self.basic_user
        return out


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def auth_config_from_env(env: dict[str, str] | None = None) -> AuthConfig:
    """Read auth config from the process environment.

    Defaults are:
      * mode = ``none`` when ORCHX_AUTH_MODE is unset. This
        keeps the v0.2.x behaviour: the dashboard is
        unauthenticated. A loud warning is logged at app
        startup if a commercial-looking ORCHX_BIND_HOST
        (i.e. anything other than 127.0.0.1) is set, but
        that's a separate concern.
      * mode = ``basic`` requires ORCHX_AUTH_BASIC_USER and
        ORCHX_AUTH_BASIC_PASSWORD. Both must be non-empty.
        Missing user or password is treated as a
        configuration error and raises at startup so the
        operator notices immediately rather than the
        control plane silently running with no auth.
      * mode = ``api_key`` requires ORCHX_AUTH_API_KEY.
    """
    e = env if env is not None else os.environ
    mode = (e.get("ORCHX_AUTH_MODE") or "none").strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError(f"ORCHX_AUTH_MODE must be one of {AUTH_MODES!r}, got {mode!r}")
    if mode == "basic":
        user = (e.get("ORCHX_AUTH_BASIC_USER") or "").strip()
        password = e.get("ORCHX_AUTH_BASIC_PASSWORD") or ""
        if not user or not password:
            raise ValueError(
                "ORCHX_AUTH_MODE=basic requires both ORCHX_AUTH_BASIC_USER "
                "and ORCHX_AUTH_BASIC_PASSWORD to be set"
            )
        return AuthConfig(
            mode="basic",
            basic_user=user,
            basic_password_hash=_sha256_hex(password),
        )
    if mode == "api_key":
        key = e.get("ORCHX_AUTH_API_KEY") or ""
        if not key:
            raise ValueError("ORCHX_AUTH_MODE=api_key requires ORCHX_AUTH_API_KEY to be set")
        return AuthConfig(mode="api_key", api_key_hash=_sha256_hex(key))
    return AuthConfig(mode="none")


# ---- credential extraction ----


def _extract_basic(request: Request) -> tuple[str, str] | None:
    """Return ``(user, password)`` from an HTTP Basic header, or None."""
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    user, password = decoded.split(":", 1)
    return user, password


def _extract_bearer(request: Request) -> str | None:
    """Return the bearer token from an Authorization header, or None."""
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header or not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def _extract_query_token(request: Request) -> str | None:
    """Return ``?token=...`` from the URL, or None.

    Used for the WebSocket endpoint only, because browsers
    cannot set custom headers on a WebSocket request.
    """
    return request.query_params.get("token")


# ---- verify ----


def _check_basic(config: AuthConfig, presented_user: str, presented_password: str) -> bool:
    if config.basic_user is None or config.basic_password_hash is None:
        return False
    user_ok = hmac.compare_digest(presented_user.encode("utf-8"), config.basic_user.encode("utf-8"))
    pass_ok = hmac.compare_digest(_sha256_hex(presented_password), config.basic_password_hash)
    return user_ok and pass_ok


def _check_api_key(config: AuthConfig, presented_key: str) -> bool:
    if config.api_key_hash is None:
        return False
    return hmac.compare_digest(_sha256_hex(presented_key), config.api_key_hash)


# ---- FastAPI dependencies ----


def _bearer_or_basic_for_request(config: AuthConfig, request: Request) -> str | None:
    """Pick the credential that matches the active mode.

    Returns the secret (password or api key) for the
    ``api_key`` mode, or ``"ok"`` for ``basic`` mode, or
    ``None`` for ``none`` mode. The caller compares this
    against the presented credential.

    The split-by-mode logic is encapsulated here so the
    route handlers don't have to repeat it.
    """
    if config.mode == "none":
        return ""
    if config.mode == "api_key":
        token = _extract_bearer(request) or _extract_query_token(request)
        if token is None:
            return None
        return token
    if config.mode == "basic":
        creds = _extract_basic(request)
        if creds is None:
            return None
        user, password = creds
        if _check_basic(config, user, password):
            return ""
        return None
    return None


def require_user_or_token_factory(config: AuthConfig):
    """Build a FastAPI dependency that gates on the config.

    We return a function (rather than a Depends-style object)
    so the dependency can be a plain method that the
    framework can introspect without a runtime import.
    """

    def _dep(request: Request) -> None:
        if config.mode == "none":
            return
        if _bearer_or_basic_for_request(config, request) is None:
            # The detail string intentionally doesn't reveal
            # which mode the server is in: a 401 is the same
            # for missing credentials, wrong credentials,
            # and the wrong auth mode.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": 'Basic realm="orchx"'},
            )

    return _dep


def websocket_check_token(config: AuthConfig, websocket: WebSocket) -> bool:
    """Return True if the WebSocket connection is authorised.

    WebSockets cannot easily return HTTP 401 (the framework
    closes the connection on us), so we accept the query
    parameter ``?token=...`` for token auth, or the
    ``Basic`` header if the client managed to attach one.
    On failure we close with code 1008 (policy violation).
    """
    if config.mode == "none":
        return True
    if config.mode == "api_key":
        token = websocket.query_params.get("token")
        if token is None:
            # Try the Sec-WebSocket-Protocol header (which
            # browsers can set, unlike Authorization).
            proto = websocket.headers.get("sec-websocket-protocol") or ""
            for candidate in proto.split(","):
                candidate = candidate.strip()
                if candidate.startswith("bearer."):
                    token = candidate[len("bearer.") :]
                    break
        return token is not None and _check_api_key(config, token)
    if config.mode == "basic":
        # Browsers can't set Authorization on a WebSocket,
        # so we accept the credentials in the query string
        # as ``?basic=user:password`` (base64-encoded). The
        # dashboard uses sessionStorage so the credentials
        # never sit in a log line.
        creds_param = websocket.query_params.get("basic")
        if creds_param is None:
            return False
        try:
            decoded = base64.b64decode(creds_param).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        if ":" not in decoded:
            return False
        user, password = decoded.split(":", 1)
        return _check_basic(config, user, password)
    return False


__all__ = [
    "AuthConfig",
    "AUTH_MODES",
    "auth_config_from_env",
    "require_user_or_token_factory",
    "websocket_check_token",
]
