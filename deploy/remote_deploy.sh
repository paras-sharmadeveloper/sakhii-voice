#!/usr/bin/env bash
# Runs on the server as the sakhii user (invoked by the GitHub workflow):
#   bash remote_deploy.sh <release-dir>
# Builds the venv for the uploaded release, points `current` at it, then
# restarts the two instances one at a time. If an instance doesn't come up
# healthy, `current` goes back to the previous release.
set -euo pipefail

ROOT=/opt/sakhii-voice
RELEASE="${1:?release dir}"
PORTS=(8800 8801)
UV="$HOME/.local/bin/uv"

cd "$RELEASE"
"$UV" venv -q -p 3.12 .venv
VIRTUAL_ENV="$RELEASE/.venv" "$UV" pip install -q -r pyproject.toml

PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"
ln -sfn "$RELEASE" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$1/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

for port in "${PORTS[@]}"; do
  echo ">> restarting sakhii-voice@$port (waits for its live calls to finish)"
  sudo /usr/bin/systemctl restart "sakhii-voice@$port"
  if ! healthy "$port"; then
    echo "!! sakhii-voice@$port unhealthy, rolling back"
    if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
      ln -sfn "$PREVIOUS" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"
      sudo /usr/bin/systemctl restart "sakhii-voice@$port"
    fi
    exit 1
  fi
done

# Keep the last 5 releases for manual rollback.
ls -1dt "$ROOT"/releases/*/ | tail -n +6 | xargs -r rm -rf
echo ">> deployed $(basename "$RELEASE")"
