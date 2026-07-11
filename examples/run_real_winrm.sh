#!/usr/bin/env bash
# Run OrchX against a real host via WinRM, end-to-end.
#
# Usage (run this on a Linux/macOS box, NOT on the Windows host):
#   USERNAME=Administrator PASSWORD=xxx \
#       bash examples/run_real_winrm.sh [host] [descriptor]
#
# Defaults: host=192.168.10.241, descriptor=sample_webapp_erp.yaml
# (the IIS/SQL/COM descriptor; the only one that uses the
#  Windows-specific steps).
#
# What it does:
#   1. Verifies python3 + uv are available.
#   2. Clones orchx at the latest tag.
#   3. Installs the project with the [real] extra (pywinrm).
#   4. Runs the bundled webapp-erp descriptor against
#      winrm://Administrator:xxx@192.168.10.241:5985 (NTLM
#      over plain HTTP, transport-encrypted not required
#      for an internal test).
#   5. Prints the run-id so you can curl /api/runs/<id>
#      for the full event log.
set -euo pipefail

HOST="${1:-192.168.10.241}"
DESCRIPTOR_NAME="${2:-sample_webapp_erp.yaml}"
USERNAME="${USERNAME:?set USERNAME=<windows admin user>}"
PASSWORD="${PASSWORD:?set PASSWORD=<windows admin password>}"
WORK_DIR="${WORK_DIR:-$HOME/orchx}"
TARGET="winrm://${USERNAME}:${PASSWORD}@${HOST}:5985"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*"; >&2; exit 1; }

# ----- 1. preflight -----
say "1/5  preflight"
for bin in python3 git; do
  command -v "$bin" >/dev/null 2>&1 || die "missing: $bin"
done
command -v uv >/dev/null 2>&1 || pip3 install --user uv
PATH=$HOME/.local/bin:$PATH

# ----- 2. network check -----
say "2/5  reachability"
python3 - "$HOST" <<'PY' || die "cannot reach $HOST:5985"
import socket, sys
host = sys.argv[1]
s = socket.socket()
s.settimeout(5)
try:
    s.connect((host, 5985))
    print(f"  tcp to {host}:5985 OK")
except OSError as e:
    print(f"  tcp to {host}:5985 FAILED: {e}")
    sys.exit(1)
PY

# ----- 3. clone or update -----
say "3/5  clone or update $WORK_DIR"
if [ ! -d "$WORK_DIR" ]; then
  git clone https://github.com/Tanyongjay/orchx.git "$WORK_DIR"
fi
cd "$WORK_DIR"
git fetch --tags
git checkout v0.3.0-beta

# ----- 4. install -----
say "4/5  install with [real,dev] extras"
uv pip install --system -e ".[real,dev]" 2>/dev/null \
  || pip3 install --user -e ".[real,dev]"

# ----- 5. real-WinRM plan + deploy -----
say "5/5  orchx plan + deploy against $HOST"
DESCRIPTOR="$WORK_DIR/descriptors/$DESCRIPTOR_NAME"
[ -f "$DESCRIPTOR" ] || die "descriptor not found: $DESCRIPTOR"

orchx plan "$DESCRIPTOR" --target "$TARGET"
echo
orchx deploy "$DESCRIPTOR" --target "$TARGET" --no-rollback

echo
echo "DONE"
