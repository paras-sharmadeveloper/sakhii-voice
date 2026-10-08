# Shared by setup.sh and update.sh. Sourced, not run.

ROOT="${SAKHII_ROOT:-/opt/sakhii-voice}"
APP="$ROOT/app"
VENV="$ROOT/venv"
ENV_FILE="$ROOT/shared/.env"
PYTHON="${PYTHON:-/bin/python3.12}"
REPO_URL="${REPO_URL:-https://github.com/paras-sharmadeveloper/sakhii-voice.git}"
BRANCH="${BRANCH:-main}"
UNIT=sakhii-voice
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
UNIT_FILE="$UNIT_DIR/$UNIT.service"
SUDOERS_FILE="${SUDOERS_DIR:-/etc/sudoers.d}/sakhii-voice"
HEALTH_URL=http://127.0.0.1:8000/healthz
HEALTH_TRIES="${HEALTH_TRIES:-30}"

require_root() {
  [ "$(id -u)" = 0 ] || { echo "!! run as root (sudo bash $0)" >&2; exit 1; }
}

# git over HTTPS. The repo is private: either export GITHUB_TOKEN (a GitHub
# token with read-only "Contents" access) or let git ask for username and
# token. The token goes to git through GIT_CONFIG_* environment variables,
# so it's never written to disk or visible in the process list.
gh_git() {
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
    GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')" \
      git "$@"
  else
    git "$@"
  fi
}

# Dependencies from pyproject.toml (the project isn't an installable package).
install_deps() {
  local reqs
  reqs="$(mktemp)"
  (cd "$APP" && "$VENV/bin/python" -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))') > "$reqs"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r "$reqs"
  rm -f "$reqs"
}

install_unit() {
  if ! cmp -s "$APP/deploy/sakhii-voice.service" "$UNIT_FILE"; then
    install -m 644 "$APP/deploy/sakhii-voice.service" "$UNIT_FILE"
    systemctl daemon-reload
  fi
}

healthy() {
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
