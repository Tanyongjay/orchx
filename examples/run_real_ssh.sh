#!/usr/bin/env bash
# Run OrchX against a real host via SSH, end-to-end.
#
# Usage (run this ON 192.168.10.241, as user `jay` with sudo):
#   bash examples/run_real_ssh.sh
#
# What it does:
#   1. Verifies git, uv, ssh, systemd are available.
#   2. Clones the orchx repo at the latest tag.
#   3. Installs the project with the [real] extra (asyncssh).
#   4. Runs the bundled sample_oauth_service.yaml descriptor
#      against ssh://jay@127.0.0.1:22?key=$HOME/.ssh/id_ed25519.
#   5. Prints the run-id so you can `orchx` look it up in the
#      dashboard (or via /api/runs/<id>).
#
# If anything fails, the script exits with a non-zero status
# and a clear "where" so you can rerun from that point.
set -euo pipefail

REPO_URL="https://github.com/Tanyongjay/orchx.git"
WORK_DIR="${WORK_DIR:-$HOME/orchx}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/id_ed25519}"
TARGET="${TARGET:-ssh://jay@127.0.0.1:22?key=$KEY_PATH}"
DESCRIPTOR="${DESCRIPTOR:-$WORK_DIR/descriptors/sample_oauth_service.yaml}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

say "1/6  preflight"
for bin in git uv ssh systemctl; do
  command -v "$bin" >/dev/null 2>&1 || die "missing: $bin"
done
[ -f "$KEY_PATH" ] || die "ssh key not found at $KEY_PATH  (set KEY_PATH=... to override)"
[ -r "$KEY_PATH" ] || die "ssh key not readable: $KEY_PATH"

say "2/6  self-test ssh"
ssh -i "$KEY_PATH" -o BatchMode=yes -o ConnectTimeout=5 jay@127.0.0.1 true \
  || die "ssh self-test failed; check that the key is in jay@127.0.0.1's authorized_keys"

say "3/6  clone or update $WORK_DIR"
if [ -d "$WORK_DIR" ]; then
  cd "$WORK_DIR" && git fetch --tags --prune
else
  git clone "$REPO_URL" "$WORK_DIR"
  cd "$WORK_DIR"
fi
LATEST_TAG=$(git tag --sort=-version:refname | head -1)
say "  latest tag: $LATEST_TAG"
git checkout -q "$LATEST_TAG"

say "4/6  install (with [real] + [dev] extras)"
uv sync --extra real --extra dev

say "5/6  plan (no I/O; renders the DAG only)"
uv run orchx plan "$DESCRIPTOR"

say "6/6  deploy against $TARGET"
uv run orchx deploy "$DESCRIPTOR" --target "$TARGET"

say "done"
say "  dashboard: uv run python -m orchx.web.app   (then open http://127.0.0.1:8000/)"
