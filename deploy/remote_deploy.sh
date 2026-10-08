#!/usr/bin/env bash
# Runs on the server as the sakhii user (invoked by the GitHub workflow):
#   bash remote_deploy.sh <release-dir>
# Checks it may restart the service, builds the venv for the uploaded
# release, points `current` at it and restarts sakhii-voice. If the restart
# fails or the engine doesn't come up healthy, `current` goes back to the
# previous release.
set -euo pipefail

ROOT="${SAKHII_ROOT:-/opt/sakhii-voice}"
RELEASE="${1:?release dir}"
PORT=8000
UNIT=sakhii-voice
UV="${UV:-$HOME/.local/bin/uv}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
SYSTEMCTL="$(command -v systemctl)"
# Interpreter chosen by bootstrap.sh (Python 3.11+).
PYTHON="$(cat "$ROOT/shared/python" 2>/dev/null || echo python3.12)"

# Preflight, before anything changes: may we restart the service without a
# password? `sudo -l <cmd>` checks the rule without running anything, but on
# some servers (sudo's listpw default) listing itself wants a password even
# though the NOPASSWD command runs fine. So fall back to `start`, which is in
# the same sudoers rule and does nothing to a service that's already running.
can_restart() {
  sudo -n -l "$SYSTEMCTL" restart "$UNIT" >/dev/null 2>&1 && return 0
  sudo -n "$SYSTEMCTL" start "$UNIT" >/dev/null 2>&1
}
if ! can_restart; then
  echo "!! sudo -n -l reports:"
  sudo -n -l 2>&1 | head -20 | sed 's/^/!!   /' || true
  echo "!! The sakhii user may not run 'sudo $SYSTEMCTL restart $UNIT'."
  echo "!! The server's sudo rule is missing or from an older bootstrap. Fix it once, as root:"
  echo "!!   sudo bash $RELEASE/deploy/bootstrap.sh --no-webserver \"\$(head -1 /home/sakhii/.ssh/authorized_keys)\""
  echo "!! then re-run this deploy. Nothing was changed; $(basename "$(readlink -f "$ROOT/current" 2>/dev/null || echo none)") is still live."
  exit 1
fi

cd "$RELEASE"
"$UV" venv -q -p "$PYTHON" .venv
VIRTUAL_ENV="$RELEASE/.venv" "$UV" pip install -q -r pyproject.toml

PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"
ln -sfn "$RELEASE" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"

healthy() {
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

rollback() {
  echo "!! $1, rolling back to $(basename "${PREVIOUS:-none}")"
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"
    sudo -n "$SYSTEMCTL" restart "$UNIT" || true
  fi
  exit 1
}

echo ">> restarting $UNIT"
sudo -n "$SYSTEMCTL" restart "$UNIT" || rollback "could not restart $UNIT"
healthy || rollback "$UNIT unhealthy on 127.0.0.1:$PORT"

# Keep the last 5 releases for manual rollback.
ls -1dt "$ROOT"/releases/*/ | tail -n +6 | xargs -r rm -rf
echo ">> deployed $(basename "$RELEASE")"
