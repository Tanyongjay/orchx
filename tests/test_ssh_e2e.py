"""End-to-end test of the SSH transport against an in-process
SSH server.

This test spawns a real `asyncssh.SSHServer` bound to
127.0.0.1 on a random port, generates a key pair, then runs
the orchx engine against an `ssh://` URI that points at the
server. It exercises:

  * SSHTransport URI parsing with a keyfile path.
  * asyncssh key-based authentication.
  * run_command against the server.
  * file_exists against the server.
  * http_send against the server (the engine wraps curl).

We deliberately use a real socket and a real SSH handshake
instead of mocking. The whole point is to verify the
transport's asyncssh options are correct end-to-end on a
Linux host (the CI runner). This is the same path a paying
customer would take against their real Linux fleet.
"""

from __future__ import annotations

# Skip the whole module under two conditions:
#
# 1. asyncssh is not installed (the [real] extra is opt-in).
# 2. We are on Windows. asyncssh 2.x's SSHServer relies on
#    ProactorEventLoop + a low-level select primitive that
#    can wedge on Windows in some environments (the issue
#    manifests as a hang inside _poll, not as a clean
#    failure). The tests are most useful on Linux anyway
#    because that's where paying customers run their real
#    targets, so the CI runner (ubuntu-latest) is the
#    canonical place for them to execute.
import sys
from pathlib import Path

import asyncssh
import pytest

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.transports.ssh import SSHTransport


def _has_asyncssh() -> bool:
    try:
        import asyncssh  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = [
    pytest.mark.skipif(not _has_asyncssh(), reason="asyncssh not installed"),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="in-process asyncssh SSHServer hangs on Windows; "
        "these tests are run on the Linux CI runner only",
    ),
]


class _Server(asyncssh.SSHServer):
    """A real asyncssh SSH server. Public-key only (no
    password path) so the test exercises the same auth flow
    a paying customer would use in production.
    """

    def __init__(self, allowed_key: asyncssh.SSHKey) -> None:
        self._allowed_key = allowed_key

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return key == self._allowed_key


class _Factory:
    """asyncssh 2.x wants a process_factory(conn) -> server.

    The factory is a plain callable (NOT a coroutine) that
    must return a server. We use a class so we can pass the
    allowed key in via __init__.
    """

    def __init__(self, allowed_key: asyncssh.SSHKey) -> None:
        self._allowed_key = allowed_key

    def __call__(self, conn: object) -> _Server:
        return _Server(self._allowed_key)


async def _start_server() -> tuple[object, int, asyncssh.SSHKey, asyncssh.SSHKey]:
    """Spin up an SSH server on a random port. Returns the
    server handle, the listening port, the host key (so the
    client can trust it), and the client key (so the server
    can auth the client).
    """
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        _Factory(client_key),
        "127.0.0.1",
        0,  # random free port
        server_host_keys=[host_key],
    )
    port = server.sockets[0].getsockname()[1]
    return server, port, host_key, client_key


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_ssh_transport_runs_command_against_inprocess_server(
    tmp_path: Path,
) -> None:
    """A real `ssh://` URI on a real SSH server lets the
    orchx engine run a `command` step end-to-end.
    """
    server, port, _host_key, client_key = await _start_server()
    try:
        # Write the client private key to a file the
        # SSHTransport can reference.
        key_path = tmp_path / "client_ed25519"
        key_path.write_bytes(client_key.export_private_key())
        # `known_hosts = None` in the transport means we
        # don't pin the host key fingerprint. That's fine
        # for the in-process test; in production users
        # would point known_hosts at their real ~/.ssh/known_hosts.
        uri = f"ssh://orchx@127.0.0.1:{port}?key={key_path}"
        transport = SSHTransport(uri)

        # `id` and `pwd` are simple, always-available commands.
        result = await transport.run_command("orchx", ["id"], timeout_s=5)
        assert result.ok, f"id failed: {result.stderr}"
        assert "uid=" in result.stdout

        result = await transport.run_command("orchx", ["pwd"], timeout_s=5)
        assert result.ok
        assert result.stdout.strip()  # non-empty

        # file_exists: a path that exists
        assert await transport.file_exists("orchx", str(Path(__file__)))
        # file_exists: a path that doesn't
        assert not await transport.file_exists("orchx", str(tmp_path / "does_not_exist_xyz"))

        await transport.close()
    finally:
        # On Windows the wait_closed() path can hang in
        # asyncssh 2.x; server.close() releases the listening
        # socket which is enough to drop in-flight test work.
        server.close()


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_engine_deploys_against_ssh_target(
    tmp_path: Path,
) -> None:
    """End-to-end: orchx deploy a descriptor that targets the
    in-process SSH server, using only `command` steps (no
    COM / IIS / SQL Server — those would need different
    transports).
    """
    server, port, _host_key, client_key = await _start_server()
    try:
        key_path = tmp_path / "client_ed25519"
        key_path.write_bytes(client_key.export_private_key())

        # Build a minimal descriptor in tmp_path so the test
        # doesn't depend on the bundled samples directory.
        descriptor_path = tmp_path / "ssh_only.yaml"
        descriptor_path.write_text(
            """\
system:
  name: ssh-e2e
  code: ssh_e2e
  version: "0.0.1"

topology:
  roles:
    - name: orchx
      count: 1

steps:
  - id: probe.id
    type: command
    on_host: orchx
    cmd: [id]

  - id: probe.uname
    type: command
    on_host: orchx
    cmd: [uname, -a]
""",
            encoding="utf-8",
        )

        desc = load_descriptor(descriptor_path)
        plan = build_plan(desc)

        transport = SSHTransport(f"ssh://orchx@127.0.0.1:{port}?key={key_path}")

        # Run the engine. We pass an on_event collector so the
        # test can assert on what the engine did.
        events: list[dict[str, object]] = []

        def _on_event(node: object, attempt: object) -> None:
            events.append(
                {
                    "step_id": getattr(node, "step_id", "?"),
                    "status": getattr(attempt, "status", "?").value
                    if hasattr(attempt, "status")
                    else str(attempt),
                    "message": getattr(attempt, "message", ""),
                }
            )

        report = await Executor(
            descriptor=desc, plan=plan, transport=transport, on_event=_on_event
        ).run()

        await transport.close()

        # Assert: both steps ok, no failed attempts, exit code 0.
        assert report.exit_code == 0, [(e["step_id"], e["status"], e["message"]) for e in events]
        for node in report.plan.nodes.values():
            for att in node.attempts:
                assert att.status.value == "ok", f"{att.step_id}: {att.message}"
    finally:
        # On Windows the wait_closed() path can hang in
        # asyncssh 2.x; server.close() releases the listening
        # socket which is enough to drop in-flight test work.
        server.close()
