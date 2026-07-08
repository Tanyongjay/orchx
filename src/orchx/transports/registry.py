"""Transport registry.

A target URI like ``mock://local`` or ``winrm://web-1`` is parsed
here and dispatched to the registered transport. Real transports
are lazy-imported only if requested, so that the base install does
not require pywinrm/asyncssh.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from orchx.transports.base import Transport


class TransportNotFoundError(LookupError):
    """Raised when a URI scheme is not registered."""


_REGISTRY: dict[str, Callable[..., Transport]] = {}


def register_transport(scheme: str, factory: Callable[..., Transport]) -> None:
    """Register a transport factory for ``scheme://...`` URIs.

    ``factory`` receives ``**parsed_kwargs`` (host, port, user, ...).
    """
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
    return factory(host=host, port=port, **kwargs)


# ---- Default registrations ----

from orchx.transports.mock import MockTransport  # noqa: E402


def _make_mock(host: str, port: int, **_kwargs: object) -> Transport:
    import os

    from orchx.transports.mock import MockConfig

    cfg = MockConfig.from_json(os.environ.get("ORCHX_MOCK_CHAOS"))
    return MockTransport(config=cfg)


register_transport("mock", _make_mock)
