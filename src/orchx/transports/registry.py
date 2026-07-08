"""Transport registry.

A target URI like ``mock://local``, ``winrm://user:pass@host:port``,
or ``ssh://user@host:port`` is parsed here and dispatched to the
registered transport. Real transports are lazy-imported only if
requested, so the base install does not require ``pywinrm`` or
``asyncssh``.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from orchx.transports.base import Transport


class TransportNotFoundError(LookupError):
    """Raised when a URI scheme is not registered."""


class TransportURIError(ValueError):
    """Raised when a target URI is malformed for its scheme."""


# A factory is a callable that takes the *original* URI and the parsed
# pieces. The original URI is preserved so transports that embed
# credentials (like winrm://user:pass@host) do not have to round-trip
# their own state.
TransportFactory = Callable[..., Transport]

_REGISTRY: dict[str, TransportFactory] = {}


def register_transport(scheme: str, factory: TransportFactory) -> None:
    """Register a transport factory for ``scheme://...`` URIs."""
    _REGISTRY[scheme] = factory


def get_transport(target: str, **kwargs: object) -> Transport:
    parsed = urlparse(target)
    scheme = parsed.scheme.lower()
    factory = _REGISTRY.get(scheme)
    if factory is None:
        raise TransportNotFoundError(
            f"no transport registered for scheme {scheme!r}. known schemes: {sorted(_REGISTRY)}"
        )
    host = parsed.hostname or ""
    port = parsed.port or 0
    return factory(
        target=target,
        host=host,
        port=port,
        parsed=parsed,
        **kwargs,
    )


# ---- Default registrations ----

from orchx.transports.mock import MockTransport  # noqa: E402


def _make_mock(
    target: str,
    host: str,
    port: int,
    parsed,
    **_kwargs: object,
) -> Transport:
    import os

    from orchx.transports.mock import MockConfig

    cfg = MockConfig.from_json(os.environ.get("ORCHX_MOCK_CHAOS"))
    return MockTransport(config=cfg)


register_transport("mock", _make_mock)


# Real transports — registered lazily so the base install does not
# require pywinrm or asyncssh. Each module is imported on first use,
# so the optional dependency is paid for only when a real-target URI
# is requested.


def _make_winrm(
    target: str,
    host: str,
    port: int,
    parsed,
    **_kwargs: object,
) -> Transport:
    from orchx.transports.winrm import WinRMTransport

    return WinRMTransport(target)


register_transport("winrm", _make_winrm)
register_transport("winrm-http", _make_winrm)


def _make_ssh(
    target: str,
    host: str,
    port: int,
    parsed,
    **_kwargs: object,
) -> Transport:
    from orchx.transports.ssh import SSHTransport

    return SSHTransport(target)


register_transport("ssh", _make_ssh)
register_transport("ssh+key", _make_ssh)
