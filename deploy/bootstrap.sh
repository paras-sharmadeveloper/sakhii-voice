#!/usr/bin/env bash
# One-time server setup, run as root. Ubuntu/Debian (apt) or RHEL-family such
# as AlmaLinux (dnf/yum).
#
#   Own web server (nginx + certbot set up here):
#     sudo bash deploy/bootstrap.sh voice.YOURDOMAIN.com "ssh-ed25519 AAAA... github-deploy"
#
#   Existing web server, e.g. Webuzo/Apache (no nginx, no certbot):
#     sudo bash deploy/bootstrap.sh --no-webserver "ssh-ed25519 AAAA... github-deploy"
#     then add deploy/apache-webuzo.conf to the domain (see README).
#
# Afterwards: fill /opt/sakhii-voice/shared/.env, then push to main.
set -euo pipefail

WEBSERVER=1
if [ "${1:-}" = "--no-webserver" ]; then
  WEBSERVER=0
  shift
fi
if [ "$WEBSERVER" = 1 ]; then
  DOMAIN="${1:?usage: bootstrap.sh <domain> <deploy public key>  |  bootstrap.sh --no-webserver <deploy public key>}"
  shift
fi
DEPLOY_PUBKEY="${1:?usage: bootstrap.sh <domain> <deploy public key>  |  bootstrap.sh --no-webserver <deploy public key>}"
ROOT=/opt/sakhii-voice
HERE="$(cd "$(dirname "$0")" && pwd)"

# --- packages -------------------------------------------------------------

if command -v apt-get >/dev/null; then
  PKG=apt
elif command -v dnf >/dev/null; then
  PKG=dnf
elif command -v yum >/dev/null; then
  PKG=yum
else
  echo "!! no apt, dnf or yum found" >&2
  exit 1
fi

if [ "$PKG" = apt ]; then
  apt-get update -y
  apt-get install -y rsync curl sudo python3
else
  "$PKG" install -y rsync curl sudo tar python3
fi

# Python 3.11+ (Pipecat's minimum); the project prefers 3.12.
py_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
PYTHON=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null && py_ok "$candidate"; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  if [ "$PKG" = apt ]; then
    apt-get install -y python3.12 || apt-get install -y python3.11
  else
    "$PKG" install -y python3.12 || "$PKG" install -y python3.11
  fi
  for candidate in python3.12 python3.11; do
    if command -v "$candidate" >/dev/null && py_ok "$candidate"; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi
[ -n "$PYTHON" ] || { echo "!! could not install Python 3.11+" >&2; exit 1; }
echo ">> using $PYTHON ($("$PYTHON" --version))"

# --- deploy user ----------------------------------------------------------

id sakhii >/dev/null 2>&1 || useradd -m -s /bin/bash sakhii
install -d -o sakhii -g sakhii -m 700 /home/sakhii/.ssh
grep -qxF "$DEPLOY_PUBKEY" /home/sakhii/.ssh/authorized_keys 2>/dev/null \
  || echo "$DEPLOY_PUBKEY" >> /home/sakhii/.ssh/authorized_keys
chown sakhii:sakhii /home/sakhii/.ssh/authorized_keys
chmod 600 /home/sakhii/.ssh/authorized_keys

# uv only manages the venv and installs packages; it uses $PYTHON.
sudo -u sakhii bash -lc 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

install -d -o sakhii -g sakhii "$ROOT" "$ROOT/releases" "$ROOT/shared"
# remote_deploy.sh builds each release's venv with this interpreter.
echo "$PYTHON" > "$ROOT/shared/python"
chown sakhii:sakhii "$ROOT/shared/python"
if [ ! -f "$ROOT/shared/.env" ]; then
  install -o sakhii -g sakhii -m 600 "$HERE/../.env.example" "$ROOT/shared/.env"
  echo ">> Edit $ROOT/shared/.env (keys, Redis, EXOTEL_WS_TOKEN) before the first deploy."
fi

# --- service ----------------------------------------------------------------

SYSTEMCTL="$(command -v systemctl)"
# The deploy user may restart only the engine.
cat > /etc/sudoers.d/sakhii-voice <<SUDO
sakhii ALL=(root) NOPASSWD: $SYSTEMCTL restart sakhii-voice@8000, $SYSTEMCTL restart sakhii-voice@8001, $SYSTEMCTL start sakhii-voice@8000, $SYSTEMCTL start sakhii-voice@8001
SUDO
chmod 440 /etc/sudoers.d/sakhii-voice
visudo -cf /etc/sudoers.d/sakhii-voice

# Servers bootstrapped before the two-instance setup ran a single
# sakhii-voice.service on 8000; retire it (this ends its live calls).
if [ -f /etc/systemd/system/sakhii-voice.service ]; then
  systemctl disable --now sakhii-voice.service || true
  rm -f /etc/systemd/system/sakhii-voice.service
fi
install -m 644 "$HERE/sakhii-voice@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sakhii-voice@8000 sakhii-voice@8001
# With a release already built (re-running bootstrap on a live server), start
# them now; otherwise the first deploy starts them.
if [ -x "$ROOT/current/.venv/bin/python" ]; then
  systemctl restart sakhii-voice@8000 sakhii-voice@8001
fi

# SELinux (RHEL-family): let the web server proxy to 127.0.0.1:8000/8001.
if command -v getenforce >/dev/null && [ "$(getenforce)" != Disabled ]; then
  setsebool -P httpd_can_network_connect 1
fi

# --- web server (skipped with --no-webserver) ----------------------------------

if [ "$WEBSERVER" = 1 ]; then
  if [ "$PKG" = apt ]; then
    apt-get install -y nginx certbot python3-certbot-nginx
  else
    "$PKG" install -y epel-release || true
    "$PKG" install -y nginx certbot python3-certbot-nginx
  fi
  if [ -d /etc/nginx/sites-available ]; then
    CONF=/etc/nginx/sites-available/sakhii-voice
    sed "s/voice.YOURDOMAIN.com/$DOMAIN/g" "$HERE/nginx-voice.conf" > "$CONF"
    ln -sf "$CONF" /etc/nginx/sites-enabled/sakhii-voice
  else
    sed "s/voice.YOURDOMAIN.com/$DOMAIN/g" "$HERE/nginx-voice.conf" > /etc/nginx/conf.d/sakhii-voice.conf
  fi
  systemctl enable --now nginx
  if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
  fi
  nginx -t && systemctl reload nginx
else
  echo ">> --no-webserver: skipped nginx and certbot. Balance wss://<domain>/ws/exotel"
  echo "   across ws://127.0.0.1:8000 and :8001 (deploy/apache-webuzo.conf)."
fi

echo ">> Bootstrap done. Add the GitHub secrets and push to main to deploy."
