"""Shared pytest fixtures."""

import pytest

from orchx.transports.mock import MockConfig, MockTransport


@pytest.fixture
def mock_transport():
    return MockTransport()


@pytest.fixture
def failing_powershell_transport():
    """A mock that fails every powershell action once before succeeding.

    Lets us assert the executor's retry behaviour.
    """
    cfg = MockConfig.from_json('{"local":[{"action":"powershell","exit_code":2,"fail_times":1}]}')
    return MockTransport(config=cfg)
