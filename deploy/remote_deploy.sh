#!/usr/bin/env bash
# Runs on the server as the sakhii user (invoked by the GitHub workflow):
#   bash remote_deploy.sh <release-dir>
# Builds the venv for the uploaded release, points `current` at it and
# restarts the engine. If a restart fails or the engine doesn't come up
# healthy, `current` goes back to the previous release.
#
# Which unit to restart comes from the server's own sudo rule, so a deploy
# works with whatever bootstrap last ran, without a root step:
#   "<systemctl> restart sakhii-voice"            -> single unit, port 8000
#   "<systemctl> restart sakhii-voice@<port>"...  -> one restart per instance
# The systemctl path is used exactly as the rule spells it (sudo matches it
# literally: /bin/systemctl and /usr/bin/systemctl are different to sudo).
set -euo pipefail

ROOT="${SAKHII_ROOT:-/opt/sakhii-voice}"
RELEASE="${1:?release dir}"
UV="${UV:-$HOME/.local/bin/uv}"
HEALTH_TRIES="${HEALTH_TRIES:-30}"
# Between instances: let a web-server balancer put the restarted one back
# in rotation (apache retry=5) before the next goes down.
REJOIN_SECS="${REJOIN_SECS:-6}"
# Interpreter chosen by bootstrap.sh (Python 3.11+).
PYTHON="$(cat "$ROOT/shared/python" 2>/dev/null || echo python3.12)"
LIVE="$(basename "$(readlink -f "$ROOT/current" 2>/dev/null || echo none)")"

# --- preflight: what may we restart? (nothing changes before this passes) ---
RULES="$(sudo -n -l 2>/dev/null || true)"
ALLOWED="$(printf '%s\n' "$RULES" | grep -oE '/[^ ,]*systemctl restart sakhii-voice(@[0-9]+)?' | sort -u || true)"
TARGETS="$(printf '%s\n' "$ALLOWED" | grep -E ' sakhii-voice$' | head -1 || true)"
if [ -z "$TARGETS" ]; then
  # One line per instance, whichever systemctl spelling the rule lists first.
  TARGETS="$(printf '%s\n' "$ALLOWED" | grep -E ' sakhii-voice@[0-9]+$' | awk '!seen[$NF]++' || true)"
fi
if [ -z "$TARGETS" ]; then
  echo "!! sudo -n -l reports:"
  printf '%s\n' "${RULES:-<nothing: no sudo rule for sakhii>}" | head -20 | sed 's/^/!!   /'
  echo "!! No 'systemctl restart sakhii-voice' rule for the sakhii user. Fix it once, as root:"
  echo "!!   sudo bash $RELEASE/deploy/bootstrap.sh --no-webserver \"\$(head -1 /home/sakhii/.ssh/authorized_keys)\""
  echo "!! then re-run this deploy. Nothing was changed; $LIVE is still live."
  exit 1
fi
echo ">> will restart: $(printf '%s\n' "$TARGETS" | awk '{print $NF}' | paste -sd' ' -)"

port_of() {  # sakhii-voice -> 8000, sakhii-voice@8001 -> 8001
  case "$1" in *@*) echo "${1##*@}" ;; *) echo 8000 ;; esac
}

healthy() {
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    curl -fsS "http://127.0.0.1:$1/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# --- build and switch -----------------------------------------------------------
cd "$RELEASE"
"$UV" venv -q -p "$PYTHON" .venv
VIRTUAL_ENV="$RELEASE/.venv" "$UV" pip install -q -r pyproject.toml

PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"
ln -sfn "$RELEASE" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"

rollback() {
  echo "!! $1, rolling back to $(basename "${PREVIOUS:-none}")"
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
    ln -sfn "$PREVIOUS" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"
    while read -r target; do
      sudo -n $target </dev/null || true
    done <<<"$TARGETS"
  fi
  exit 1
}

# --- restart, one unit at a time ------------------------------------------------
first=1
while read -r target; do
  unit="${target##* }"
  port="$(port_of "$unit")"
  [ "$first" = 1 ] || sleep "$REJOIN_SECS"
  first=0
  echo ">> restarting $unit"
  sudo -n $target </dev/null || rollback "could not restart $unit"
  healthy "$port" </dev/null || rollback "$unit unhealthy on 127.0.0.1:$port"
done <<<"$TARGETS"

# Keep the last 5 releases for manual rollback.
ls -1dt "$ROOT"/releases/*/ | tail -n +6 | xargs -r rm -rf
echo ">> deployed $(basename "$RELEASE")"
