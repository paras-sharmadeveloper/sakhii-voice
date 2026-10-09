#!/usr/bin/env bash
# One-time server setup, as root (AlmaLinux 9 + Webuzo Apache). It lives in
# the repo, so clone first (private repo: git asks for your GitHub username
# and a read-only token as the password), then run it from the clone:
#
#   git clone https://github.com/paras-sharmadeveloper/sakhii-voice.git /opt/sakhii-voice/app
#   sudo bash /opt/sakhii-voice/app/deploy/setup.sh
#
# Pulls the repo in /opt/sakhii-voice/app, builds the venv with Python 3.12 in
# /opt/sakhii-voice/venv, replaces any older sakhii-voice units with one
# sakhii-voice.service on 127.0.0.1:8000, starts it and checks /healthz.
# /opt/sakhii-voice/shared/.env is never overwritten. Safe to re-run.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_root

# 1. Code and venv first, while the old service keeps answering calls.
[ -x "$PYTHON" ] || { echo "!! $PYTHON not found (set PYTHON=/path/to/python3.12)" >&2; exit 1; }
id sakhii >/dev/null 2>&1 || useradd --system --create-home --shell /sbin/nologin sakhii
install -d -m 755 "$ROOT" "$ROOT/shared"
if [ -d "$APP/.git" ]; then
  echo ">> updating $APP"
  gh_git -C "$APP" pull --ff-only
else
  echo ">> cloning $REPO_URL into $APP"
  gh_git clone --branch "$BRANCH" "$REPO_URL" "$APP"
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo ">> creating venv with $PYTHON"
  "$PYTHON" -m venv "$VENV"
fi
echo ">> installing dependencies"
install_deps

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o sakhii -g sakhii "$APP/.env.example" "$ENV_FILE"
  echo "!! Created $ENV_FILE from .env.example. Fill in REDIS_*, REDIS_KEY_PREFIX and"
  echo "!! SAKHII_VOICE_CRED_KEY (the rest can come from the admin panel), then run this script again."
  exit 1
fi

# 2. Swap services: remove every older unit and the old deploy sudo rule.
echo ">> removing old units"
for unit in sakhii-voice.service sakhii-voice@8000.service sakhii-voice@8001.service; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done
rm -f "$UNIT_DIR/sakhii-voice.service" "$UNIT_DIR/sakhii-voice@.service" "$SUDOERS_FILE"
systemctl daemon-reload
systemctl reset-failed 'sakhii-voice*' >/dev/null 2>&1 || true

# 3. One unit, started and checked.
install_unit
systemctl enable --now "$UNIT"
echo ">> waiting for $HEALTH_URL"
if ! healthy; then
  echo "!! $UNIT is not healthy. Last 50 log lines:"
  journalctl -u "$UNIT" -n 50 --no-pager || true
  exit 1
fi
echo ">> $UNIT healthy: $(curl -fsS "$HEALTH_URL")"

# 4. Leftovers from the old GitHub Actions deploy (old code copies only).
rm -rf "$ROOT/releases" "$ROOT/current" "$ROOT/current.new"
echo ">> done. Code: $APP ($(git -C "$APP" log -1 --format='%h %s'))"
echo ">> Updates from now on: sudo bash $APP/deploy/update.sh"
