"""Run OrchX against a real host via SSH using password auth.

This is the Python equivalent of examples/run_real_ssh.sh
but uses paramiko so the operator doesn't have to install
sshpass on the host running this script.

Usage (on a Linux/macOS/Windows box with paramiko installed):
    python examples/run_real_ssh.py <host> <user> <password>

Or, after setting ORCHX_TEST_SSH_HOST, ORCHX_TEST_SSH_USER,
ORCHX_TEST_SSH_PASSWORD env vars.

The script:
  1. Connects to <host>:22 via paramiko (no key needed).
  2. Verifies the toolchain on the target (git, python3, uv).
  3. Clones or updates the orchx repo at v0.3.0-beta.
  4. Installs orchx with [real,dev] extras.
  5. Sets the secrets env (throwaway values; the oauth-svc
     descriptor only validates that secrets resolve).
  6. Runs `orchx plan` and `orchx deploy` against
     ssh://<user>@<host>:22.
  7. Prints stdout/stderr of every step.

This script is intended for CI smoke tests and developer
machines. In production the operator uses a real key pair.
"""

from __future__ import annotations

import os
import sys

import paramiko

REPO_URL = "https://github.com/Tanyongjay/orchx.git"
WORK_DIR_REMOTE = os.environ.get("WORK_DIR_REMOTE", "orchx")
TARGET_TAG = "v0.3.0-beta"


def _ssh_exec(client: paramiko.SSHClient, cmd: str, *, check: bool = True) -> str:
    print(f"\n$ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out:
        print(out, end="")
    if err:
        print(f"[stderr] {err}", end="")
    rc = stdout.channel.recv_exit_status()
    if check and rc != 0:
        raise SystemExit(f"command failed (rc={rc}): {cmd}")
    return out


def main() -> int:
    if len(sys.argv) >= 4:
        host, user, password = sys.argv[1], sys.argv[2], sys.argv[3]
    else:
        host = os.environ["ORCHX_TEST_SSH_HOST"]
        user = os.environ["ORCHX_TEST_SSH_USER"]
        password = os.environ["ORCHX_TEST_SSH_PASSWORD"]
    print(f"Target: ssh://{user}@{host}:22  (via paramiko)")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=22,
        username=user,
        password=password,
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    print("SSH connected.")

    # 1. toolchain probe
    # git + python3 are required; pip3 may be absent on
    # Ubuntu 24.04+ where the python3-pip package is
    # opt-in. uv is preferred and is installed via the
    # standalone installer (curl -LsSf https://astral.sh/uv/...)
    # if it's not already on PATH.
    out = _ssh_exec(
        client,
        "set -e; "
        "for b in git python3 curl; do "
        "  command -v $b >/dev/null 2>&1 || { echo missing: $b; exit 1; }; "
        "done; "
        "if ! command -v uv >/dev/null 2>&1; then "
        "  echo installing-uv; "
        "  curl -LsSf https://astral.sh/uv/install.sh | sh; "
        "fi; "
        "export PATH=$HOME/.local/bin:$PATH; "
        "command -v uv >/dev/null || { echo uv-still-missing; exit 1; }; "
        "echo toolchain-ok",
    )
    assert "toolchain-ok" in out, out

    # 2. clone or update
    _ssh_exec(
        client,
        f"set -e; export PATH=$HOME/.local/bin:$PATH; "
        f"if [ ! -d {WORK_DIR_REMOTE} ]; then "
        f"  git clone {REPO_URL} {WORK_DIR_REMOTE}; "
        f"fi; "
        f"cd {WORK_DIR_REMOTE}; git fetch --tags; git checkout {TARGET_TAG}; "
        f"git describe --tags --always",
    )

    # 3. install
    # Install into a project-local virtualenv and put
    # ``orchx`` on PATH via the venv's bin dir. We avoid
    # ``uv pip install --system`` because Ubuntu 24.04+
    # marks the system Python as ``EXTERNALLY-MANAGED`` and
    # ``uv`` refuses to install into it. The venv gives
    # us a clean, isolated install.
    _ssh_exec(
        client,
        f"set -e; export PATH=$HOME/.local/bin:$PATH; "
        f"cd $HOME; "
        f"if [ ! -d .orchx-venv ]; then uv venv .orchx-venv; fi; "
        f"source .orchx-venv/bin/activate; "
        f"cd {WORK_DIR_REMOTE}; "
        f"uv pip install -e .[real,dev]; "
        f"orchx --help >/dev/null && echo orchx-ok",
    )

    # 4. plan
    print("\n" + "=" * 60)
    print("=== orchx plan (real SSH) ===")
    print("=" * 60)
    _ssh_exec(
        client,
        f"set -e; export PATH=$HOME/.local/bin:$PATH; "
        f"source $HOME/.orchx-venv/bin/activate; "
        f"cd {WORK_DIR_REMOTE}; "
        f"export ORCHX_SECRET_db_host=db.internal; "
        f"export ORCHX_SECRET_db_name=hr_svc; "
        f"export ORCHX_SECRET_db_user=hr_ro; "
        f"export ORCHX_SECRET_db_password=demo; "
        f"unset ORCHX_SECRET_db_port; "
        f"orchx plan descriptors/sample_settle_eod.yaml "
        f"--target ssh://{user}@127.0.0.1:22",
    )

    # 5. deploy
    print("\n" + "=" * 60)
    print("=== orchx deploy (real SSH, --no-rollback) ===")
    print("=" * 60)
    _ssh_exec(
        client,
        f"set -e; export PATH=$HOME/.local/bin:$PATH; "
        f"source $HOME/.orchx-venv/bin/activate; "
        f"cd {WORK_DIR_REMOTE}; "
        f"export ORCHX_SECRET_db_host=db.internal; "
        f"export ORCHX_SECRET_db_name=hr_svc; "
        f"export ORCHX_SECRET_db_user=hr_ro; "
        f"export ORCHX_SECRET_db_password=demo; "
        f"unset ORCHX_SECRET_db_port; "
        f"orchx deploy descriptors/sample_settle_eod.yaml "
        f"--target ssh://{user}@127.0.0.1:22 --no-rollback",
    )

    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
