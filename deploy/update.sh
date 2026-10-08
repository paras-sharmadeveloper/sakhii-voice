#!/usr/bin/env bash
# Deploy the latest main, as root:
#   sudo bash /opt/sakhii-voice/app/deploy/update.sh
#   (private repo: sudo GITHUB_TOKEN=<read-only token> bash ...)
#
# git pull, reinstall deps, restart sakhii-voice, wait for /healthz. If it
# isn't healthy, go back to the previous commit, restart again, and show the
# last 50 journal lines. A restart ends live calls; update when it's quiet.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_root

cd "$APP"
PREV="$(git rev-parse HEAD)"
echo ">> pulling $BRANCH (now at $(git log -1 --format='%h %s'))"
gh_git pull --ff-only
NEW="$(git rev-parse HEAD)"
[ "$NEW" = "$PREV" ] && echo ">> already up to date; restarting anyway"

install_deps
install_unit
echo ">> restarting $UNIT at $(git log -1 --format='%h %s')"
systemctl restart "$UNIT"
if healthy; then
  echo ">> $UNIT healthy: $(curl -fsS "$HEALTH_URL")"
  exit 0
fi

echo "!! $UNIT unhealthy after the update; going back to $(git log -1 --format='%h %s' "$PREV")"
git reset --hard "$PREV"
install_deps
install_unit
systemctl restart "$UNIT"
if healthy; then
  echo ">> rolled back; $UNIT healthy on the previous commit"
else
  echo "!! $UNIT still unhealthy after the rollback"
fi
echo "!! Last 50 log lines:"
journalctl -u "$UNIT" -n 50 --no-pager || true
exit 1
