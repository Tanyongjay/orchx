"""Transport abstractions and concrete implementations.

The transport is the only thing that differentiates "this runs against
the mock" from "this runs against a real Windows host". Steps emit
standardised actions; the transport decides how to deliver them to the
host.
"""

from orchx.transports.base import (
    CommandResult,
    FileTransfer,
    HttpSendRequest,
    HttpSendResult,
    IisSiteSpec,
    RegisterOptions,
    SqlRunResult,
    Transport,
)
from orchx.transports.mock import MockTransport
from orchx.transports.registry import get_transport, register_transport

__all__ = [
    "Transport",
    "CommandResult",
    "FileTransfer",
    "RegisterOptions",
    "IisSiteSpec",
    "SqlRunResult",
    "HttpSendRequest",
    "HttpSendResult",
    "MockTransport",
    "get_transport",
    "register_transport",
]
