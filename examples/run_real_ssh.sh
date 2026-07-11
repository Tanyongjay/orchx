#!/usr/bin/env bash
# Run OrchX against a real host via SSH using password auth.
#
# Usage (run on a Linux/macOS box with sshpass installed):
#   USERNAME=jay PASSWORD='***' bash examples/run_real_ssh.sh [host]
#
# Default host: 192.168.10.241 (the LAN-attached Ubuntu box).
# This script uses sshpass so it works against an Ubuntu host
# that has password auth enabled (the default for OpenSSH on
# Ubuntu Desktop) without the operator having to copy a key
# pair to the target first.
#
# What it does:
#   1. Verifies python3 + uv are available.
#   2. Verifies sshpass is available (sudo apt install sshpass).
#   3. Clones orchx at the latest tag into a workdir on the
#      TARGET host (so we don't have to ship the source over
#      SSH every time).
#   4. Installs orchx with the [real,dev] extras on the target.
#   5. Runs `orchx plan` and `orchx deploy` against
#      ssh://jay@<host>:22.
#   6. Prints the run-id so the operator can curl the local
#      control plane for the event log.
set -euo pipefail

HOST="${1:-192.168.10.241}"
USERNAME="${USERNAME:?set USERNAME=<ssh user>}"
PASSWORD="${PASSWORD:?set PASSWORD=<ssh password>}"
WORK_DIR_REMOTE="${WORK_DIR_REMOTE:-$HOME/orchx}"
TARGET="ssh://${USERNAME}@${HOST}:22"
DESCRIPTOR_NAME="${DESCRIPTOR_NAME:-sample_oauth_service.yaml}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*"; >&2; exit 1; }

# ----- 1. local preflight -----
say "1/7  local preflight"
for bin in python3 ssh sshpass; do
  command -v "$bin" >/dev/null 2>&1 || die "missing: $bin  (try: sudo apt install $bin)"
done

# ----- 2. ssh self-test (password) -----
say "2/7  ssh self-test -> $HOST as $USERNAME"
sshpass -p "$PASSWORD" ssh \
    -o StrictHostKeyChecking=accept-new \
    -o ConnectTimeout=10 \
    -o BatchMode=no \
    "$USERNAME@$HOST" "echo ssh-ok" >/dev/null \
  || die "ssh self-test failed; check USERNAME/PASSWORD and that the host is reachable"

# ----- 3. check that the target has the build toolchain -----
say "3/7  target toolchain (git + python3 + uv)"
sshpass -p "$PASSWORD" ssh -o BatchMode=no "$USERNAME@$HOST" bash -s <<'REMOTE' || die "target toolchain probe failed"
set -e
for bin in git python3; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "missing on target: $bin"
    exit 1
  }
done
# uv: try to install if missing.
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found on target; installing"
  pip3 install --user uv 2>/dev/null || sudo apt install -y python3-pip && pip3 install --user uv
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "toolchain OK"
REMOTE

# ----- 4. clone or update orchx on the target -----
say "4/7  clone or update $WORK_DIR_REMOTE on $HOST"
sshpass -p "$PASSWORD" ssh -o BatchMode=no "$USERNAME@$HOST" bash -s <<REMOTE || die "clone failed"
set -e
export PATH="\$HOME/.local/bin:\$PATH"
if [ ! -d "$WORK_DIR_REMOTE" ]; then
  git clone https://github.com/Tanyongjay/orchx.git "$WORK_DIR_REMOTE"
fi
cd "$WORK_DIR_REMOTE"
git fetch --tags
git checkout v0.3.0-beta
echo "checked out \$(git describe --tags --always)"
REMOTE

# ----- 5. install orchx on the target -----
say "5/7  install orchx on $HOST with [real,dev] extras"
sshpass -p "$PASSWORD" ssh -o BatchMode=no "$USERNAME@$HOST" bash -s <<REMOTE || die "install failed"
set -e
export PATH="\$HOME/.local/bin:\$PATH"
cd "$WORK_DIR_REMOTE"
uv pip install --system -e ".[real,dev]" 2>/dev/null \
  || pip3 install --user -e ".[real,dev]"
echo "installed \$(orchx --help 2>&1 | head -3)"
REMOTE

# ----- 6. secrets env -----
say "6/7  secrets env"
# The oauth-svc descriptor needs DB credentials. We use
# throwaway values because the descriptor only validates
# that the secrets resolve; the SQL steps don't actually
# connect to a database under mock transport.
SECRET_ENV=$(cat <<'ENV'
export ORCHX_SECRET_db_host=db.internal
export ORCHX_SECRET_db_name=hr_svc
export ORCHX_SECRET_db_user=hr_ro
export ORCHX_SECRET_db_password=demo
unset ORCHX_SECRET_db_port
ENV
)
sshpass -p "$PASSWORD" ssh -o BatchMode=no "$USERNAME@$HOST" "export PATH=\$HOME/.local/bin:\$PATH; cd $WORK_DIR_REMOTE; bash -lc '$SECRET_ENV'"

# ----- 7. real-SSH plan + deploy -----
say "7/7  orchx plan + deploy against $HOST"
sshpass -p "$PASSWORD" ssh -o BatchMode=no "$USERNAME@$HOST" bash -lc "
  set -e
  export PATH=\$HOME/.local/bin:\$PATH
  export ORCHX_SECRET_db_host=db.internal
  export ORCHX_SECRET_db_name=hr_svc
  export ORCHX_SECRET_db_user=hr_ro
  export ORCHX_SECRET_db_password=demo
  unset ORCHX_SECRET_db_port
  cd $WORK_DIR_REMOTE
  echo '--- plan ---'
  orchx plan descriptors/$DESCRIPTOR_NAME --target $TARGET
  echo
  echo '--- deploy ---'
  orchx deploy descriptors/$DESCRIPTOR_NAME --target $TARGET --no-rollback
"

echo
echo "DONE"
