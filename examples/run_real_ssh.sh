#!/usr/bin/env bash
# Run this on 192.168.10.241 as user jay (with sudo).
#
# What it does:
#   1. Installs git/python/uv/ssh.
#   2. Generates an ed25519 key (idempotent) and adds the
#      public key to authorized_keys so ssh to localhost
#      works without a password.
#   3. Clones orchx at the v0.3.0-beta tag and installs it
#      with [real,web,dev] extras.
#   4. Sets up the secrets env so the oauth-svc descriptor
#      can resolve its database credentials.
#   5. Runs `orchx plan` and `orchx deploy` against
#      ssh://jay@127.0.0.1:22?key=...
#
# If any step fails, the script exits non-zero with a clear
# "where". Re-run from that point.
set -euo pipefail

# ----- 1. deps -----
command -v sudo >/dev/null || { echo "sudo not found"; exit 1; }
command -v apt >/dev/null && {
  sudo apt update
  sudo apt install -y git python3 python3-pip openssh-client
}

# ----- 2. uv -----
command -v uv >/dev/null || pip3 install --user uv
PATH=$HOME/.local/bin:$PATH

# ----- 3. ssh key (idempotent) -----
mkdir -p ~/.ssh
chmod 700 ~/.ssh
if [ ! -f ~/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
fi
grep -q -F "$(cat ~/.ssh/id_ed25519.pub)" ~/.ssh/authorized_keys 2>/dev/null \
  || cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# ----- 4. ssh self-test -----
echo
echo "=== ssh self-test ==="
ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes jay@127.0.0.1 true \
  && echo "ssh OK" || { echo "ssh FAILED"; exit 1; }

# ----- 5. clone + install orchx -----
if [ ! -d ~/orchx ]; then
  git clone https://github.com/Tanyongjay/orchx.git ~/orchx
fi
cd ~/orchx
git fetch --tags
git checkout v0.3.0-beta

# Install (try system, fall back to user)
uv pip install --system -e ".[real,web,dev]" 2>/dev/null \
  || pip3 install --user -e ".[real,web,dev]"

# ----- 6. secrets env (override as needed) -----
export ORCHX_SECRET_db_host=db.internal
export ORCHX_SECRET_db_name=hr_svc
export ORCHX_SECRET_db_user=hr_ro
export ORCHX_SECRET_db_password=demo
unset ORCHX_SECRET_db_port

# ----- 7. real-ssh plan + deploy -----
echo
echo "=== orchx plan (real SSH) ==="
orchx plan descriptors/sample_oauth_service.yaml \
    --target "ssh://jay@127.0.0.1:22?key=$HOME/.ssh/id_ed25519"

echo
echo "=== orchx deploy (real SSH, --no-rollback) ==="
orchx deploy descriptors/sample_oauth_service.yaml \
    --target "ssh://jay@127.0.0.1:22?key=$HOME/.ssh/id_ed25519" \
    --no-rollback

echo
echo "DONE"
