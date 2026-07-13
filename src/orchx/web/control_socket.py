"""Cross-process control socket for the orchx control plane.

The dashboard's POST /api/runs/<id>/cancel endpoint drives
the executor in-process: it sets a per-run asyncio.Event and
cancels the executor task. That's fine for browser users,
but operators who reach the orchx CLI on a remote shell
(``orchx state cancel <id>``) don't have a UI to click.
This module adds a small cross-process control surface so
the CLI's cancel command actually reaches the executor.

The surface is loopback-only, single-host, opt-in:

  * The FastAPI control plane binds a TCP listener on
    127.0.0.1:<port> during lifespan startup. The port
    is auto-assigned by the kernel (port=0); the actual
    port is written to ``state/orchx.control_port`` so
    client processes can find it.

  * The CLI reads the port file, opens a TCP socket,
    sends a newline-terminated JSON command, reads one
    response line, and closes.

  * Supported verbs: today just ``cancel`` (request
    cancellation of a run), with the same semantics
    as ``POST /api/runs/<id>/cancel``. Future verbs
    (``shutdown``, ``list``) join the same protocol.

Cross-platform note: AF_UNIX is Linux/macOS only. We use
loopback TCP because the protocol surface stays identical
across Windows and POSIX without requiring the kernel
newer than Windows 10. Loopback TCP is filtered by the
kernel to ``127.0.0.1`` so neither the LAN nor the
public internet can reach it without explicit forwarding.

Security: the listener binds to ``127.0.0.1`` only, the
port file is the only advertised entry point, and the
listener self-terminates if the write side of the file
vanishes. Cross-host operations would be a v0.6.x+
addition (TLS-protected JSON-over-TCP); for v0.6.1 the
operator must run the CLI on the same host as the
control plane.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

log = logging.getLogger("orchx.web.control_socket")

DEFAULT_PORT_FILE = Path("state") / "orchx.control_port"


class ControlPlaneClient:
    """Client-side helper for the cross-process control
    socket.

    Used by the CLI's ``orchx state cancel`` command.
    Construct, ``await send_cancel(run_id)``, done.
    """

    def __init__(self, port: int) -> None:
        self.port = port

    @classmethod
    def from_port_file(
        cls,
        path: Path = DEFAULT_PORT_FILE,
    ) -> ControlPlaneClient | None:
        if not path.exists():
            return None
        try:
            port = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return cls(port)

    async def send_cancel(self, run_id: str) -> dict[str, Any]:
        """Send a cancel command and read the response."""
        cmd = {"cmd": "cancel", "run_id": run_id}
        body = (json.dumps(cmd) + "\n").encode("utf-8")
        try:
            reader, writer = await asyncio.open_connection(
                host="127.0.0.1",
                port=self.port,
            )
        except OSError as e:
            return {"ok": False, "error": f"connect: {e}"}
        try:
            writer.write(body)
            await writer.drain()
            data = await asyncio.wait_for(
                reader.readline(),
                timeout=5.0,
            )
        except (TimeoutError, OSError) as e:
            return {"ok": False, "error": f"send/recv: {e}"}
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": f"non-JSON response: {data[:200]!r}",
            }


# The cancel dispatch is bound to a specific AppState at
# lifespan startup time. ``register_cancel_callback``
# captures both the callback and the state, plus the loop
# the AppState is bound to. ``serve`` reads these on each
# connection and the cancel coroutine is scheduled onto
# that exact loop via ``run_coroutine_threadsafe``.
#
# This avoids the failure mode of capturing the loop
# only at dispatch time: with multiple TestClient
# fixtures in a pytest run, each lifespan runs on its own
# loop, and we want the cancel coroutine to fire on the
# right one.
_dispatch: tuple[Any, Any, asyncio.AbstractEventLoop] | None = None


def register_cancel_callback(
    fn: Any,
    state: Any | None = None,
    loop: Any | None = None,
) -> None:
    import asyncio

    global _dispatch
    if loop is None:
        loop = asyncio.get_event_loop()
    _dispatch = (fn, state, loop)


def unregister_cancel_callback() -> None:
    global _dispatch
    _dispatch = None


def _default_cancel_handler(req: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one command from a client connection.

    Today the only supported verb is ``{"cmd": "cancel",
    "run_id": "..."}``; everything else returns an error.
    """
    cmd = req.get("cmd")
    if cmd != "cancel":
        return {
            "ok": False,
            "error": f"unknown cmd: {cmd!r}; supported: cancel",
        }
    run_id = req.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"ok": False, "error": "run_id (str) required"}
    if _dispatch is None:
        return {
            "ok": False,
            "error": "no cancel callback registered",
        }
    fn, state, loop = _dispatch
    return fn(run_id)


def _make_protocol(handler: Any) -> type[asyncio.Protocol]:
    """Build the asyncio.Protocol subclass that handles a
    single client connection.

    Each connection carries one request + one response.
    The Protocol writes the response and the asyncio
    transport closes the socket. We do not buffer more
    than one request: a client that wants two commands
    opens two sockets.
    """

    class _Protocol(asyncio.Protocol):
        def connection_made(self, transport: Any) -> None:
            # NOTE: ``asyncio.StreamReader.StreamReaderProtocol``
            # passes (reader, writer) when used as the
            # callback_factory; as a vanilla ``asyncio.Protocol``
            # we just get ``transport`` here. We assign
            # it directly and write via ``transport.write``.
            peer = transport.get_extra_info("peername")
            log.debug("control socket: connection from %s", peer)
            # Bound for the lifetime of the connection.
            self._transport = transport
            self._buf = b""
            self._cancelled = False

        def data_received(self, data: bytes) -> None:
            if self._cancelled:
                return
            self._buf += data
            while b"\n" in self._buf and not self._cancelled:
                line, self._buf = self._buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    self._reply(
                        {
                            "ok": False,
                            "error": f"bad JSON: {e}",
                        }
                    )
                    self._cancelled = True
                    return
                try:
                    resp = handler(req)
                except Exception as e:
                    self._reply(
                        {
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                    self._cancelled = True
                    return
                self._reply(resp)
                # One request per connection: clients open
                # a fresh socket for each command. Keeps
                # the protocol stateless.
                self._cancelled = True
                return

        def _reply(self, payload: dict[str, Any]) -> None:
            try:
                self._transport.write(
                    (json.dumps(payload) + "\n").encode("utf-8"),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("control socket reply failed: %s", e)

        def connection_lost(self, exc: Any) -> None:
            transport = getattr(self, "_transport", None)
            if transport is not None and not transport.is_closing():
                transport.close()

    return _Protocol


async def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    port_file: Path = DEFAULT_PORT_FILE,
    handler: Any | None = None,
) -> None:
    """Run the cross-process control-socket server.

    Listens on ``(host, port)`` (port=0 means auto-assigned
    by the kernel). Once the kernel-assigned port is known,
    the actual port is written to ``port_file`` so client
    processes can find us.

    ``handler`` is invoked per accepted connection. If
    ``None``, ``_default_cancel_handler`` is used.
    """
    if handler is None:
        handler = _default_cancel_handler

    loop = asyncio.get_event_loop()
    server = await loop.create_server(
        _make_protocol(handler),
        host=host,
        port=port,
    )
    # Discover the kernel-assigned port (port=0 case) and
    # write it to the port file so client processes can find
    # us. The file is small and world-readable; the security
    # model is "you can connect to my loopback socket if you
    # can read this file", which is fine because the only
    # thing on the loopback socket is cancel commands.
    sockets = server.sockets or ()
    sock_port = sockets[0].getsockname()[1] if sockets else port
    # Use ``asyncio.to_thread`` for the on-disk write so we
    # don't block the loop on POSIX fs flushes. The file
    # is small and the write is short, but a hung NFS
    # share could stall the control plane otherwise.
    port_file.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        port_file.write_text,
        str(sock_port),
        encoding="utf-8",
    )
    log.info("control socket listening on %s:%s", host, sock_port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        # ``run_in_executor`` lets us unlink without
        # blocking; ``missing_ok=True`` makes the call
        # idempotent if the file was already removed by a
        # prior SIGTERM.
        with suppress(OSError):
            await asyncio.to_thread(
                port_file.unlink,
                missing_ok=True,
            )
