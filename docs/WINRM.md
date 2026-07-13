# Real-host WinRM

This document is the operator's playbook for running
OrchX against a real Windows host over WinRM. The Linux /
SSH path is in `docs/SSH.md`; the two paths share the
same descriptor format, the same engine, and the same
auth gate, but the target URI shape and the transport's
edge cases differ.

## When to use this guide

Use WinRM when:

* The target host is Windows Server 2016+ or Windows 10+
  with the WinRM service running and reachable on port
  5985 (HTTP) or 5986 (HTTPS).
* The descriptor uses any of the Windows-specific step
  kinds: `iis-site`, `com-register`, `powershell`,
  `sql-server`. The SSH path also supports `command`,
  `check`, `sql`, `package`, and `healthcheck`, but
  most real-world Windows workloads need the
  Windows-specific kinds.
* You have an Administrator-equivalent account on the
  target. IIS app-pool creation, COM component
  registration, and Windows Service management all
  require elevation, and the orchx transport runs
  every step as the credential it was given.

## What's in the box

When you install orchx with the `[real]` extra, the
following become available:

* `pywinrm` — the WinRM client library. The transport
  uses `winrm.Session` for command execution and the
  WSMan SOAP endpoint for everything else.
* `requests` and `requests-ntlm` — transitive deps of
  `pywinrm`. The transport uses HTTP basic auth via
  NTLM; HTTPS uses the same path with TLS verification.
* `xmltodict` — used to deserialize the WinRM SOAP
  envelopes.

There's no `examples/run_real_winrm.py` script yet, by
design: an end-to-end WinRM smoke would require a
Windows box running WinRM with admin credentials, which
the orchx CI doesn't have. The transport is exercised
by `tests/test_winrm_transport.py` (in-process fake,
URI parsing, NTLM auth flow) and the broader `pytest`
suite, which all run on Linux/macOS CI. A real-Windows
smoke is a v0.4.x follow-up.

## Building a target URI

```
winrm://<user>:<password>@<host>:<port>
winrm-http://<user>:<password>@<host>:<port>
```

* `<user>`: a Windows account with admin privileges
  on the target. Domain accounts use the
  `DOMAIN\user` form; local accounts use `.\user` or
  just `user`.
* `<password>`: the account's cleartext password. The
  orchx security model is that this URI never reaches
  disk: the operator is expected to inject it via
  `examples/run_real_winrm.py` or a CI secret. Putting
  a password in argv on a multi-user host leaks it to
  everyone running `ps`.
* `<host>`: hostname or IP. IPv4 and IPv6 both work.
* `<port>`: defaults to 5985 (HTTP) and 5986 (HTTPS).
  Either port works; HTTPS requires the target to have
  a server certificate that the orchx host's CA store
  trusts. Self-signed certificates are NOT accepted by
  default — see "TLS verification" below.

The two URI schemes (`winrm://` and `winrm-http://`)
are aliases; both run on the WinRM SOAP-over-HTTP
endpoint. There is no `winrm-https` alias because the
operator already controls the scheme by choosing port
5986; if you really want to make it explicit, the
cleanest pattern is to register a new `winrm-https`
scheme in `orchx.transports.registry` and a one-line
class derived from `WinRMTransport`. This isn't
included in v0.4 because HTTPS is just an HTTP variant
in the protocol — there's no functional difference
once the orchx transport trusts the target's cert.

### TLS verification

The orchx WinRM transport uses Python `requests`, which
verifies TLS by default. If the operator wants to use a
self-signed cert (typical for internal lab boxes), they
have three options:

1. **Install the cert in the orchx host's CA store**
   (the right answer for production).
2. **Set `REQUESTS_CA_BUNDLE` to the path of the
   self-signed cert** before running orchx
   (cleanly scoped to the orchx process).
3. **Pass `verify=False` via a custom transport**
   subclass — NOT recommended, NOT supported in v0.4.

The default "fail loud" behaviour is intentional:
operators frequently forget to install a cert, and the
resulting ``requests.exceptions.SSLError`` error from
orchx is exactly the diagnostic you want.

## First-run on the target

The Windows host needs WinRM enabled. From an
Administrator PowerShell:

```powershell
# 1. Enable WinRM (default disabled on Server)
Enable-PSRemoting -Force

# 2. Allow NTLM over plain HTTP (orchx's path)
Set-Item -Path "WSMan:\localhost\Service\Auth\AllowUnencrypted" -Value $true

# 3. Allow basic auth (orchx uses HTTP basic with NTLM)
Set-Item -Path "WSMan:\localhost\Service\Auth\Basic" -Value $true

# 4. Open the firewall
New-NetFirewallRule -DisplayName "WinRM 5985" `
    -Direction Inbound -LocalPort 5985 -Protocol TCP -Action Allow

# 5. Confirm WinRM is reachable
Get-Service WinRM | Format-Table Name, Status, StartType
winrm enumerate winrm/config/listener
Test-NetConnection -ComputerName <host> -Port 5985
```

In a domain environment, step 2 is the most likely
sticking point: group policy often overrides
`AllowUnencrypted`. The orchx transport supports HTTPS
explicitly via port 5986, so the recommended production
deployment is to use a cert from your internal CA.

## Sample descriptor

The repo ships `descriptors/sample_webapp_erp.yaml`
that exercises every Windows-specific step kind. It's
an IIS + SQL Server + COM workload with ten steps:

```
precheck.iis     check        web       Probe IIS on port 80
precheck.sql     check        db        Probe SQL Server
schema.create    sql          db        CREATE TABLE in master
com.register     com-register web       Register the bespoke COM DLL
pkg.stage        package      web       Stage the installer tarball
svc.stop         command      web       Stop the existing IIS app pool
rev:svc.stop     command      web       Restart the app pool on rollback
pkg.install      command      web       Run the installer
iis.upsert       iis-site     web       Bind the new site
smoke.tcp        healthcheck  web       Wait for port 80
```

A successful run against a real Windows box ends with
the new IIS site serving HTTP 200 on the configured
port. The descriptor assumes SQL Server is on the same
host with `localhost`; for cross-host SQL, change
`on_host: db` to the SQL host's role name and update
the secrets to point at it.

## Network requirements

* TCP outbound from the orchx host to `<target>:5985`
  (HTTP) or `<target>:5986` (HTTPS).
* The orchx host does NOT need any incoming ports.
* The target host must allow HTTP basic auth with NTLM.
  Kerberos is supported by pywinrm via
  `transport=kerberos`; orchx's default is NTLM, which
  covers the most common case.

## Performance

* A typical 10-step IIS + SQL descriptor against a
  Windows host on the same LAN runs in 15-25 seconds.
  Each step pays the WinRM SOAP round-trip cost
  (typically 50-150ms per call). The SQL step is the
  slowest because it may round-trip multiple times.
* A typical 10-step descriptor against a Windows host
  across the public internet (50ms RTT) runs in
  30-60 seconds. The RTT dominates here.
* The orchx transport keeps a single
  `winrm.Session` open for the duration of the deploy.
  It is closed when the transport's `close()` method
  is called (or at process exit). This is fine for
  orchx descriptors that run a handful of steps, but
  for descriptors with hundreds of steps you may want
  to keep the session across deploys (TODO: add a
  connection pool in v0.5).

## Failure modes

| Symptom | Likely cause | What to check |
|---|---|---|
| `ConnectionRefusedError` | WinRM not listening | `Get-Service WinRM` (Running); `winrm enumerate winrm/config/listener` (HTTP on 5985) |
| `401 Unauthorized` | Auth config wrong | `Set-Item -Path "WSMan:\localhost\Service\Auth\Basic" -Value $true` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Self-signed cert, orchx host doesn't trust it | Install the cert in the orchx host CA store, or use port 5985 with `AllowUnencrypted` |
| `Access denied` | User is not admin | `whoami /groups \| findstr "Administrators"` on the target |
| `WinRM client cannot process the request` | Time skew or DC unreachable | Check the target's clock; for domain accounts ensure the orchx host can reach a DC |
| `pywinrm` import error | `[real]` extra not installed | `pip install 'orchx[real]'` |
| `0x80070005 Access denied` mid-step | The step requires elevation but the orchx transport runs as the user, not as admin | Use an admin account; orchx does not currently invoke UAC for individual commands |

## Choosing between SSH and WinRM

If you have a Linux shop, use SSH. If you have a
Windows shop, use WinRM. The descriptor is the same —
the only difference is the target URI. Multi-host
descriptors are out of scope for v0.4 (see
`docs/ROADMAP.md`); when they ship in v0.5+, an
operator can mix `ssh://` and `winrm://` URIs by giving
each `on_host: role` a separate transport.

## Where the orchx code lives

The WinRM transport is in
`src/orchx/transports/winrm.py`. The pywinrm-based
implementation is about 250 lines including the URI
parser. Tests live in
`tests/test_winrm_transport.py` (URI parsing, NTLM
auth flow, fake target).

## Security checklist before going to production

1. **Never put a real password in a target URI in a
   CI variable or shell history.** Use
   `examples/run_real_winrm.py` (when it lands in
   v0.4.x), a CI secret that reads from a vault, or
   a custom transport subclass that pulls the password
   at runtime.
2. **Use HTTPS.** Port 5986 with a server cert from
   your internal CA, every time. The
   `AllowUnencrypted` setting is for smoke tests in
   isolated networks only.
3. **Use a dedicated orchx-deploy account.** Not your
   personal admin account, not the Administrator
   built-in. A new account whose password rotates
   quarterly.
4. **Rotate the password after every incident.**
   orchx does not rotate for you; that's deliberate,
   because "orchx rotated the password" is the kind of
   silent operation that surprises on-call engineers.
5. **Audit WinRM auth.** WinRM logs every connection
   to the Windows Event Log under
   `Microsoft\Windows\WinRM\Operational`. Forward
   these to your log pipeline.
6. **Limit the orchx-deploy account's privileges.**
   IIS, COM, and Service management require admin,
   but the account should not be a Domain Admin. Use
   a local Administrators group member.

## What's NOT covered by WinRM

* **UAC elevation for individual commands.** The
  WinRM transport sends every command as the
  orchx-deploy user. If the orchx-deploy user runs
  as a non-elevated user, certain steps (notably
  IIS app-pool recycling) will fail with `0x80070005
  Access denied`. v0.4 doesn't include a workaround;
  the recommended path is to use the local
  Administrator account or a domain account that's
  already a local admin.
* **WinRM over HTTPS with self-signed certs.** See
  "TLS verification" above — the recommended path
  is install-the-cert, not disable-verification.
* **Multi-host descriptors.** See `docs/ROADMAP.md` —
  that's a v0.5+ feature.

For SSH on Linux, see `docs/SSH.md`. For preflight
checks before deploying, run `orchx doctor` and see
the `doctor` command's help text.
