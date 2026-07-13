"""Top-level pytest configuration for orchx tests.

Pytest discovers this file automatically. We use it to
(a) make sure the cross-process control socket is OFF
during the in-process TestClient suite, so the fixture's
lifespan teardown doesn't race with a TCP listener that
holds a reference to the loop's task registry.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_control_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disable the loopback control socket during tests.

    The socket is opt-in in production
    (``ORCHX_CONTROL_DISABLED != "1"``); running tests
    under that default would start a TCP listener whose
    Task object holds a reference to the test's loop
    after the fixture's lifespan ends, and that future
    becomes a `concurrent.futures.CancelledError` on the
    next test.

    ``tests/test_control_socket.py`` overrides this by
    starting its own listener with a caller-supplied
    port (no port-file write, no autouse disable needed).
    """
    monkeypatch.setenv("ORCHX_CONTROL_DISABLED", "1")
