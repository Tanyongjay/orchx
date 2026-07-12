# Real-host SSH

This document is the operator's playbook for running
OrchX against a real Linux host over SSH. The same
playbook covers the Windows / WinRM path with a few
small substitutions (see `docs/WINRM.md` for that variant).

## When to use this guide

Use SSH when:

* The target host is Linux (Ubuntu 20.04+, Debian 11+,
  RHEL 8+, Rocky 8+, Alpine 3.16+) and has OpenSSH server
  installed and reachable on port 22.
* You have a deploy account on the target with
  `sudo -n` privileges (recommended) or root access via
  the SSH transport.
* The descriptor uses only `command`, `check`, `sql`,
  `package`, and `healthcheck` step kinds. For the
  Windows-specific kinds (`iis-site`, `com-register`,
  `powershell`) you need the WinRM path.

## What's in the box

When you install orchx with the `[real]` extra, the
following become available:

* `asyncssh` — the SSH client library. The transport
  uses asyncssh's `conn.run()` for command execution
  and its SFTP subsystem for `transfer_files`.
* `pywinrm` — pulled in for the WinRM transport, but
  not required for SSH-only deployments.
* `paramiko` — a transitive dep of `asyncssh`. It is
  also what `examples/run_real_ssh.py` uses to drive
  the end-to-end smoke test.

## Building a target URI

```
ssh://<user>@<host>:<port>?key=<path>[&key_passphrase=<pw>]
```

* `<user>`: the SSH user. The orchx transport runs the
  remote commands as this user. `sudo -n` (NOPASSWD)
  is the recommended way to grant root privileges
  inside the descriptor.
* `<host>`: hostname or IP. IPv4 and IPv6 both work.
* `<port>`: defaults to 22.
* `key=<path>`: path on the local filesystem of the
  SSH private key. Required unless you also pass a
  password via the runtime environment (see below).
* `key_passphrase=<pw>`: passphrase for an encrypted
  private key. Optional; only required if the key file
  is itself encrypted.

### Password-only auth

orchx does not currently embed a password in the
target URI. The reason: passwords in `argv` show up in
process listings and the orchx `plan` table. Instead,
use one of:

1. The `examples/run_real_ssh.py` driver, which takes
   the password as a function argument and never logs
   it. This is the recommended path for the first-run
   smoke test and for CI.
2. An SSH key pair (preferred for production).
3. The `ORCHX_SSH_PASSWORD` env var, read by a custom
   fork. (Not in upstream orchx; we recommend the
   driver or a key pair.)

## Smoke-testing a host

The recommended first-run is:

```
# 1. Make sure SSH itself works
ssh user@host true

# 2. Make sure orchx can drive the same user
orchx deploy descriptors/sample_ssh_smoke.yaml \
    --target ssh://user@host:22
```

`sample_ssh_smoke.yaml` is the smallest descriptor in
the box: four `command` steps (`whoami`, `hostname`,
`uname -r`, `df -h /`) and zero platform assumptions.
A successful run proves the orchx engine, the SSH
transport, and the descriptor loader are all wired
together correctly.

The expected output is `4 ok, 0 failed` with the rich
UI, or `exit_code: 0` and a JSON RunReport on stdout
if you pass `--json`.

## Running a real workload

`sample_oauth_service.yaml` and `sample_settle_eod.yaml`
are the two real-workload sample descriptors. They both
need root-level operations (creating system users,
writing to `/etc/cron.d/`, managing systemd units).
The recommended setup:

1. Create a dedicated deploy account on the target:
   `useradd -m orchx-deploy`.
2. Grant the account NOPASSWD sudo via
   `/etc/sudoers.d/orchx-deploy`:
   ```
   orchx-deploy ALL=(ALL) NOPASSWD: ALL
   ```
   In production, scope this down to the specific
   commands the descriptors run (e.g.
   `useradd`, `systemctl`, `tee /etc/cron.d/...`).
3. Add the orchx-deploy public key to
   `~orchx-deploy/.ssh/authorized_keys`.
4. Run the descriptor from your laptop:
   ```
   orchx deploy descriptors/sample_oauth_service.yaml \
       --target ssh://orchx-deploy@host:22
   ```

The `sudo -n` invocations in the descriptor will succeed
because the sudoers file grants NOPASSWD.

## Network requirements

* TCP outbound from the orchx host to `<target>:22`.
  Most firewalls allow this by default; the orchx
  host does not need any incoming ports.
* The target host must allow SSH key auth (or password
  auth, via the driver). The orchx transport supports
  the OpenSSH `MaxAuthTries` and `MaxSessions` settings
  with their OpenSSH defaults.
* The orchx transport does NOT need `AllowTcpForwarding`
  or `PermitTunnel`. SSH port forwarding is not used.
* The orchx transport does NOT need a TTY (`RequestTTY`
  is `no` in the underlying call). It does NOT need a
  pty. Pure non-interactive command execution.

## Performance

* A typical 10-step descriptor against a Linux host on
  the same LAN runs in 2-5 seconds. SSH handshakes
  dominate the first call; subsequent calls reuse the
  underlying asyncssh connection.
* A typical 10-step descriptor against a Linux host
  across the public internet (50ms RTT) runs in 5-15
  seconds. Most of that is round-trip latency.
* The orchx transport keeps the SSH connection open
  for the duration of the deploy. It is closed when
  the transport's `close()` method is called (or at
  process exit). This is the right default for
  descriptors that run a handful of steps, but for
  descriptors with hundreds of steps you may want to
  keep the connection across deploys (TODO: add a
  connection pool in v0.5).

## Failure modes

| Symptom | Likely cause | What to check |
|---|---|---|
| `ConnectionRefusedError` | SSH not listening | `ss -tlnp \| grep 22` on target |
| `Authentication failed` | Wrong key / wrong user | `ssh -i <key> -v user@host true` |
| `Host key verification failed` | First connect; new key | Accept the host key, or pre-populate known_hosts |
| `Permission denied (publickey)` | Key not in authorized_keys | Check the key fingerprint matches what the orchx host is sending |
| `Permission denied (command)` | Target command not in sudoers | `sudo -n <command>` as the orchx-deploy user |
| Timeout | Firewall drop | `telnet host 22` from the orchx host |
| `no such file or directory` | install_root wrong, or path on target missing | SSH in and check by hand |

## Where the orchx code lives

The SSH transport is in
`src/orchx/transports/ssh.py`. The asyncssh-based
implementation is about 350 lines including the URI
parser. Tests live in `tests/test_ssh_transport.py`
(URI parsing, no live host) and `tests/test_ssh_e2e.py`
(real end-to-end, Linux-only by design).

## What's NOT covered by SSH

* `com-register` and `iis-site` steps are Windows-only.
* `powershell` is Windows-only.
* Running as a non-root user without `sudo` works for
  read-only descriptors (`check`, `healthcheck`, `sql`
  against an already-configured database) but NOT for
  the workload samples that need to install services.

For Windows, see `docs/WINRM.md`. For a multi-host
DAG, see `ROADMAP.md` — that's a v0.5+ feature.
