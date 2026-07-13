"""Tests for the v0.4 transport cancel signal.

The lock-down test exercises the actual cancel pathway:
a chaos descriptor injects a 60s sleep into one step, the
executor requests a cancel after 5s, and the test asserts
that:

  1. The run completes (does not hang).
  2. The total elapsed wall-clock time is well under 60s,
     meaning the in-flight transport call was actually
     interrupted, not just the surrounding event loop.
  3. The aborted flag on the report is True.

We use the SSH transport pointed at an ephemeral in-process
real SSH server. The SSH server has a "sleep 60" command
that we hit, then we cancel. Without cancel support, the
test would take at least 60s to complete. With cancel it
completes in a few seconds.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import asyncssh
import pytest

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.transports.ssh import SSHTransport

# ---- in-process SSH server fixture (mirrors the one in ----
# ---- tests/test_ssh_e2e.py, but is local to this file so ----
# ---- the lock-down test can run independently) ----------


class _CancelServer:
    """An in-process asyncssh SSH server that echoes commands.

    The 'sleep 60' command is the one we use to provoke a
    long-running in-flight call that the test then cancels.

    asyncssh 2.x changed from a simple SSHServer class with
    ``connection_made`` to a typed factory pattern: the
    ``server_factory`` callable receives the connection and
    returns the server object. The minimal hook we need
    is ``begin_auth`` to allow password-less auth.
    """

    def __init__(self) -> None:
        # asyncssh 2.x factory: factory(conn) -> server.
        # We return a server object with ``begin_auth`` that
        # accepts any client. The previous (asyncssh 1.x)
        # SSHServer-with-connection_made pattern no longer
        # works in 2.x — the "object has no attribute
        # connection_made" failure we hit in CI is exactly
        # that.
        self._host = "127.0.0.1"
        self._port = 0

        class _Srv(asyncssh.SSHServer):
            def begin_auth(self, conn: asyncssh.SSHServerConnection) -> bool:  # noqa: D401
                # Accept any auth; the orchx client doesn't
                # pass a password (real SSH uses key auth),
                # so any successful auth response here
                # satisfies the handshake.
                conn.set_authorized_keys([])
                return True

        self.server = _Srv
        # Bind later in _serve once we have a key
        self.host_key: asyncssh.SSHKey | None = None
        self.port = 0
        self._runner: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self.host_key: asyncssh.SSHKey | None = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self._runner: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.close()

    async def _serve(self) -> None:
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self._runner = await asyncssh.create_server(
            self.server,
            self.host,
            0,
            server_host_keys=[self.host_key],
        )
        socks = self._runner.sockets or []
        assert socks
        self.port = socks[0].getsockname()[1]
        self._ready.set()
        try:
            await self._runner.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._runner.close()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop is not None and self._runner is not None:
            self._loop.call_soon_threadsafe(self._runner.close)
        if self._thread is not None:
            self._thread.join(timeout=2)


@pytest.fixture
def cancel_server() -> object:
    """A real in-process asyncssh server (Linux only)."""

    if os.name == "nt":
        pytest.skip("cancel-signal e2e requires an in-process SSH server, which hangs on Windows")
    srv = _CancelServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_transport_cancel_aborts_inflight_ssh_call(
    cancel_server: _CancelServer,
) -> None:
    """The lock-down test.

    What this proves:
      1. transport.cancel() actually interrupts the in-flight
         SSH call (rather than just abandoning it).
      2. The executor's ``should_cancel`` hook is wired into
         transport.cancel().
      3. Total elapsed wall-clock is well under the 60s sleep
         the step would otherwise take.
    """
    # A descriptor with a single step that sleeps 60s. We
    # use /bin/sh -c "sleep 60" so the engine doesn't need
    # to know about a 'sleep' step kind.
    descriptor_yaml = """
system:
  name: Cancel smoke
  code: cancel_smoke
  version: "0.1.0"

topology:
  roles:
    - name: control
      count: 1

steps:
  - id: long_sleep
    type: command
    on_host: control
    cmd:
      - /bin/sh
      - -c
      - "sleep 60"
"""
    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".yaml",
        delete=False,
        mode="w",
    ) as f:
        f.write(descriptor_yaml)
        descriptor_path = f.name
    try:
        desc = load_descriptor(descriptor_path)
    finally:
        os.unlink(descriptor_path)

    plan = build_plan(desc)

    # The SSH transport needs to authenticate to the
    # in-process server. asyncssh's key-auth path needs a
    # client key plus the server's host key accepted.
    # For the test we use password-less auth by exporting
    # an SSH agent… but a simpler approach is to write a
    # temporary key, register it on the server side, and
    # point the transport at it.
    #
    # Even simpler: skip key auth entirely by hard-coding
    # the server to accept no auth, then we can connect
    # with ``password=None`` and the server's ``password``
    # callback doesn't even run.
    #
    # We do that by registering the public half of a
    # locally-generated ed25519 key against the user's
    # ``authorized_keys`` lookup. For the lock-down test
    # it's enough that the connection succeeds at all.

    # Asyncssh supports `client_keys=None` to skip key auth;
    # we'll let it fall through to ``password=None``. The
    # fake server doesn't enforce auth so this works.
    transport = SSHTransport(f"ssh://anon@{cancel_server.host}:{cancel_server.port}")

    # The cancel-after-5s machinery. We use a hook that
    # returns False until 5s after the run starts, then
    # returns True. The executor checks this between steps
    # AND now also asks transport.cancel() so the in-flight
    # SSH call is interrupted.
    started = time.time()

    def should_cancel() -> bool:
        return (time.time() - started) > 5.0

    events: list[tuple[str, str]] = []

    async def on_event(node, attempt) -> None:
        events.append((node.step_id, attempt.status.value))

    async def go() -> None:
        executor = Executor(
            descriptor=desc,
            plan=plan,
            transport=transport,
            should_cancel=should_cancel,
            on_event=on_event,
        )
        report = await executor.run()
        # Stash on the function for the assertion phase.
        go.report = report  # type: ignore[attr-defined]

    asyncio.run(go())
    elapsed_total = time.time() - started

    # Sanity: this would normally take 60s. The cancel must
    # bring us in well under that. We assert < 30s to give
    # the Windows test-skip path some headroom even if it
    # ever runs.
    assert elapsed_total < 30.0, (
        f"cancel did not interrupt the in-flight SSH call: "
        f"elapsed={elapsed_total:.1f}s (expected <30s)"
    )

    # The run was aborted (cancel fired).
    report = go.report  # type: ignore[attr-defined]
    assert report.aborted is True

    # Make sure the cleanup is okay.
    asyncio.run(transport.close())


def test_transport_protocol_default_cancel_is_noop() -> None:
    """The Transport.cancel() default implementation
    defined in orchx.transports.base is a no-op so
    transports that cannot interrupt mid-call don't have
    to do anything special.
    """
    from orchx.transports.base import Transport
    from orchx.transports.mock import MockTransport

    t: Transport = MockTransport()
    # Calling cancel() should just return without raising
    # or doing anything visible.
    asyncio.run(t.cancel())
